"""
core/orchestrator.py
====================
Orchestrator

The central coordinator that ties every subsystem together:

    LogWatcher → DiagnosticAgent → SolverAgent → UI

Flow
----
1.  Start the file watcher in a background thread.
2.  Block on the error queue in the main thread.
3.  For each :class:`~core.watcher.ErrorEvent`:

    Block 1 – show error, ask user to Confirm or Ignore.
    Block 2 – generate diagnostic commands, ask user to Allow or Deny,
               execute them, collect output.
    Block 3 – generate fix commands (optionally loop once for extra
               diagnostics), show colour-coded table, ask user to Apply
               or Skip, execute one-by-one with status display.
"""

from __future__ import annotations

import queue
import json
from uuid import uuid4
from pathlib import Path

import config
from agents.diagnostic import DiagnosticAgent
from agents.solver import SolverAgent
from core.diagnostic_commands import validate_diagnostic_commands
from core.history import HistoryStore
from core.error_context import validate_solution_context
from core.executor import run_command, run_commands_collect
from core.watcher import ErrorEvent, LogWatcher
from ui.display import Display
from core.system_context import system_context, file_facts, solution_conditions, check_conditions, reported_services
from core.reporting import SessionReport
from core.event_grouping import EventInbox
from core.monitoring import MonitorState
from core.knowledge import assessment, redact, knowledge_card, bounded_context, local_check_results
from core.linux_library import evaluate as evaluate_linux, resolution as linux_resolution


class RequestCancelled(Exception):
    pass


class Orchestrator:
    """
    Initialises all subsystems from :mod:`config` and runs the main event
    loop.  Call :meth:`run` to start.
    """

    def __init__(self) -> None:
        paths = self._load_paths(config.LOGS_CONFIG_FILE)
        self._watcher = LogWatcher(paths, config.ERROR_PATTERNS, rules_path=config.WATCHDOG_RULES_FILE)
        self._diagnostic = DiagnosticAgent(
            config.OLLAMA_MODEL, config.OLLAMA_BASE_URL, config.OLLAMA_TIMEOUT
        )
        self._solver = SolverAgent(
            config.OLLAMA_MODEL, config.OLLAMA_BASE_URL, config.OLLAMA_TIMEOUT
        )
        self._ui = Display()
        self._paths = paths
        self._enhanced = True
        self._system = system_context()
        self._session = None
        self._checks = []
        self._inbox = EventInbox(self._watcher.error_queue)
        self._monitor_state = MonitorState(config.RUNTIME_PATH, paths, config.WATCHDOG_RULES_FILE, self._watcher.error_queue)
        self._watcher._handler.event_callback = self._monitor_state.detected
        self._monitor_state.rules_provider = self._watcher._handler.rules_status
        self._history = None
        if config.HISTORY_ENABLED:
            try:
                self._history = HistoryStore(config.CHROMA_PATH, context=self._system)
            except Exception as exc:
                self._ui.error(f"History unavailable; continuing without storage: {exc}")

    # ──────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Start the file watcher and enter the blocking event loop.
        Exits cleanly on KeyboardInterrupt (Ctrl+C).
        """
        self._watcher.start()
        state = getattr(self, '_monitor_state', None)
        if state:
            try:
                state.start()
            except OSError as exc:
                self._ui.error(f'Monitoring status unavailable: {exc}')
        self._ui.show_startup(self._paths)
        monitor = self._ui.monitoring(self._watcher.error_queue) if getattr(self, '_enhanced', False) else None
        if monitor is not None:
            monitor.start()

        try:
            while True:
                try:
                    if getattr(self, '_enhanced', False):
                        batch = self._inbox.next_batch(group_window_seconds=300)
                        event = batch[0]
                        self._batch = batch
                        if len(batch) > 1:
                            self._ui.info(f"This error repeated {len(batch)} times; reviewing once and retaining every occurrence.")
                    else:
                        event = self._watcher.error_queue.get(timeout=0.5)
                    if monitor is not None:
                        monitor.stop()
                    if state:
                        state.update(pending_count=len(self._inbox.pending), pending=[{'error': e.line, 'source': e.filepath, 'detected': e.timestamp} for e in list(self._inbox.pending)[:30]])
                    self._handle_error(event)
                    if not getattr(self, '_enhanced', False):
                        self._ui.info("Continuing monitoring for new errors…")
                    if monitor is not None:
                        monitor.start()
                except queue.Empty:
                    continue  # No new events; keep polling
        except EOFError:
            self._ui.info("Console input closed. Stopping without an ignore decision. Run ./run.sh in an interactive terminal.")
        except KeyboardInterrupt:
            self._ui.show_goodbye()
        finally:
            if monitor is not None:
                monitor.stop()
            self._watcher.stop()
            if state:
                state.stop()

    # ──────────────────────────────────────────────────────────────────
    # Pipeline
    # ──────────────────────────────────────────────────────────────────

    def _handle_error(self, event: ErrorEvent) -> None:
        if not getattr(self, '_enhanced', False):
            return self._process_error(event)
        self._session = SessionReport(event, dict(self._system, services=reported_services(event.line)))
        with self._ui.investigation(event, self._session.data):
            self._review_error(event)

    def _review_error(self, event: ErrorEvent) -> None:
        self._ignored = False
        self._automated = False
        self._facts = file_facts(event.line)
        self._conditions = solution_conditions(event.line, self._facts, self._system)
        self._checks = []
        self._last_verdict = None
        self._hypothesis = event.line
        self._active_occurrences = []
        self._attempt_id = None
        self._captures = []
        self._evidence_captures = []
        self._verification_checks = []
        self._trusted_checks = []
        self._manual_steps = []
        self._history_references = []
        self._selected_reference = None
        self._session.data.update(schema_version=2, model=config.OLLAMA_MODEL)
        self._session.add('file_facts_before', self._facts)
        self._session.add('repeated_occurrences', len(getattr(self, '_batch', [event])))
        self._session.data['incident_count'] = len(getattr(self, '_batch', [event]))
        try:
            self._stage('Detected', 'Review this error or ignore this occurrence')
            history = getattr(self, '_history', None)
            if history:
                from core.problem_actions import process
                self._automated = process(history, event, self._session, getattr(self, '_batch', [event]))
                if self._automated:
                    self._active_occurrences = list(self._session.data['occurrence_ids'])
            if not self._automated:
                self._process_error(event)
        except RequestCancelled:
            self._session.data['outcome'] = 'cancelled'
            self._session.add('cancelled', 'User cancelled the LLM request')
            self._ui.info('Current error cancelled; monitoring continues.')
        except (KeyboardInterrupt, EOFError):
            self._session.data['outcome'] = 'cancelled'
            self._note('cancelled', 'Terminal input closed or monitoring interrupted')
            raise
        finally:
            self._retain_repeats(event)
            self._session.finish()
            self._persist_case()
            self._ui.show_case_summary(self._session.data)
            self._ui.set_status('Monitoring')
            state = getattr(self, '_monitor_state', None)
            if state:
                state.update(stage='Monitoring', current_case=None)
        if self._ignored or self._automated:
            return
        if self._ui.ask_export():
            try:
                report = self._session.export(config.REPORTS_PATH)
                self._ui.info(f'Report saved: {report}')
            except OSError as exc:
                self._ui.error(f'Could not export report: {exc}')

    def _ignore_error(self, message="Error ignored."):
        self._ignored = True
        if getattr(self, '_session', None):
            self._session.data['outcome'] = 'ignored'
        self._note('resolution', 'User ignored this error')
        self._ui.info(message)

    def _note(self, stage, data):
        session = getattr(self, '_session', None)
        if session is not None:
            data = redact(data)
            session.add(stage, data)
            if stage == 'diagnostic_plan':
                session.data.update(cause=data['hypothesis'], conclusion='Suspected')
                session.data['cause_assessment'] = {'status':'inconclusive', 'description':data['hypothesis'], 'explanation':'Current hypothesis awaits evidence'}
            elif stage == 'diagnostic_conclusion':
                current = assessment(data)
                session.data.update(symptom_assessment=current['symptom'], cause_assessment=current['cause'],
                                    conclusion={'confirmed': 'Confirmed by evidence', 'disproved': 'Rejected hypothesis or symptom not reproduced', 'inconclusive': 'Inconclusive'}[data['status']])
                session.data['cause'] = current['cause'].get('description') or session.data['cause']
            elif stage == 'after_fix_conclusion':
                session.data['verification'] = data
            elif stage == 'reused_solution':
                session.data.update(cause=data.get('notes') or 'Historical solution; verify the current result', conclusion='Suspected')
            elif stage == 'saved_solution':
                session.data['solution_id'] = data['solution_id']
                if data.get('automatically_checked'):
                    session.data['verification_solution_id'] = data['solution_id']
            self._persist_case()

    def _persist_case(self):
        session = getattr(self, '_session', None)
        history = getattr(self, '_history', None)
        if session and history and session.data.get('error_key'):
            try:
                history.save_investigation(session.data)
            except Exception as exc:
                self._ui.error(f'Could not save investigation: {exc}')

    def _stage(self, stage, next_step):
        session = getattr(self, '_session', None)
        if session:
            session.data.update(stage=stage, next_step=next_step)
            session.add('pipeline_stage', {'stage': stage, 'next_step': next_step})
            self._ui.show_pipeline(stage, next_step, session.data['case_number'])
            self._persist_case()
            state = getattr(self, '_monitor_state', None)
            if state:
                state.update(stage=stage, current_case={key: session.data[key] for key in ('id', 'case_number', 'error', 'stage', 'next_step', 'conclusion')})

    def _collect_diagnostics(self, commands, event, note):
        details = []
        output = run_commands_collect(commands, timeout=config.COMMAND_TIMEOUT,
                                      max_output_chars=config.MAX_OUTPUT_CHARS,
                                      max_total_chars=config.MAX_DIAGNOSTIC_TOTAL_CHARS,
                                      context=event.line, capture=lambda item: details.append(redact(item)))
        output = redact(output)
        self._captures = details
        prior = {item['command']: item for item in getattr(self, '_evidence_captures', [])}
        prior.update({item['command']: item for item in details})
        self._evidence_captures = list(prior.values())
        self._ui.show_command_output(output)
        self._note(note, output)
        if details:
            self._note(note + '_complete', details)
            self._ui.show_full_evidence_option(details)
        return output

    def _retain_repeats(self, event):
        inbox = getattr(self, '_inbox', None)
        session = getattr(self, '_session', None)
        if not inbox or not session:
            return
        repeats = inbox.collect_repeats(event, window_seconds=300)
        if not repeats:
            return
        session.add('additional_occurrences', len(repeats))
        session.data['incident_count'] += len(repeats)
        history = getattr(self, '_history', None)
        if history and session.data.get('error_key'):
            for repeat in repeats:
                try:
                    _, occurrence = history.record_occurrence(repeat)
                    self._active_occurrences.append(occurrence)
                    session.data['occurrence_ids'].append(occurrence)
                except Exception as exc:
                    self._ui.error(f'Could not save repeated occurrence: {exc}')
        self._ui.info(f'Incident grouped {len(repeats)} additional identical events within five minutes. Their outcomes remain unreported.')
        state = getattr(self, '_monitor_state', None)
        if state:
            state.update(pending_count=len(inbox.pending), pending=[{'error': e.line, 'source': e.filepath, 'detected': e.timestamp} for e in list(inbox.pending)[:30]])

    def _llm(self, operation, label):
        if not getattr(self, '_enhanced', False):
            return operation()
        while True:
            try:
                with self._ui.activity(label):
                    return operation()
            except (RuntimeError, ValueError, KeyboardInterrupt) as exc:
                self._note('request_failed_or_cancelled', str(exc) or 'Interrupted')
                self._ui.error(str(exc) or 'Request interrupted')
                if not self._ui.ask_retry():
                    raise RequestCancelled from exc

    def _process_error(self, event: ErrorEvent) -> None:
        """Drive one reviewed error through history, evidence, fixes and outcome."""

        history = getattr(self, "_history", None)
        key = occurrence = None
        context = ""
        self._library_plan = None
        self._selected_reference = None
        if getattr(self, '_enhanced', False):
            context = 'Current system and file facts (data only):\n' + json.dumps({'system': dict(self._system, services=reported_services(event.line)), 'files': self._facts})
        if history is not None:
            self._ui.thinking("Loading local error and solution history…")
            try:
                key, occurrence = history.record_occurrence(event)
                if getattr(self, '_session', None):
                    self._session.data.update(error_key=key, occurrence_ids=[occurrence])
                    prior = history.case_records(key)
                    resolved = next((case for case in prior if case['id'] != self._session.data['id'] and case['status'] == 'resolved'), None)
                    if resolved:
                        self._session.data['recurrence_of_case'] = resolved['case_number']
                        self._ui.info('This error appeared again after ' + resolved['case_number'] + ' was marked resolved. Fresh checks are required.')
                if getattr(self, '_enhanced', False):
                    self._active_occurrences = [occurrence]
                    for repeated in getattr(self, '_batch', [event])[1:]:
                        _, additional = history.record_occurrence(repeated)
                        self._active_occurrences.append(additional)
                        self._session.data['occurrence_ids'].append(additional)
            except Exception as exc:
                self._ui.error(f"History write failed; continuing without storage: {exc}")
                history = None

        # ── Block 1: Error ────────────────────────────────────────────
        confirmed = self._ui.show_error_panel(event)
        if not confirmed:
            self._ignore_error()
            return

        if history is not None:
            try:
                summary = history.summary(key)
                related = history.related(event.line, key, include_other_environments=True)
            except Exception as exc:
                self._ui.error(f"History lookup failed; continuing diagnostics: {exc}")
                summary, related = {"seen": 1, "solved": 0, "solutions": []}, []
            if getattr(self, '_enhanced', False):
                for item in summary['solutions']:
                    item['applicable'], item['applicability_reason'] = check_conditions(item.get('conditions', {}), event.line, self._facts, self._system)
                    try:
                        validate_solution_context(event.line, item.get('commands', []))
                    except ValueError as exc:
                        item['applicable'], item['applicability_reason'] = False, str(exc)
            self._stage('History checked', 'Diagnostic templates are reference only; establish the current symptom and cause before suggesting repairs')
            try:
                scenarios = history.diagnostic_candidates(event.line)
            except Exception as exc:
                self._ui.error('Diagnostic scenario lookup unavailable; using fresh checks: ' + str(exc))
                scenarios = []
            compact_scenarios = [{key: item.get(key) for key in ('id', 'title', 'category', 'commands', 'verification_checks', 'diagnosis', 'environment', 'application', 'application_version', 'source')} for item in scenarios]
            self._trusted_checks = [check for item in scenarios if item.get('source') == 'user'
                and item.get('environment', {}).get('machine_id') == getattr(self, '_system', {}).get('machine_id')
                and item.get('environment', {}).get('os') == getattr(self, '_system', {}).get('os')
                and (not item.get('application') or item['application'].casefold() in event.line.casefold())
                and (not item.get('application_version') or item['application_version'] in event.line)
                for check in item.get('verification_checks', [])]
            context += "\nHistorical read-only diagnostic templates (not current evidence):\n" + bounded_context({
                'diagnostic_scenarios': compact_scenarios})

        # ── Block 2: Diagnostic ───────────────────────────────────────
        combined_diag = self._diagnose_error(event, context)
        if combined_diag is None:
            return

        if history is not None:
            candidates = self._ui.show_history(summary, related)
            self._stage('Diagnosing', 'Symptom and cause confirmed; choose historical reference for the proposed repair')
            self._note('history_matches', {'exact': summary, 'related': related})
            action, selected = self._ui.choose_history_action(candidates)
            while action == 'manage':
                try:
                    self._ui.manage_history(history)
                    summary = history.summary(key)
                    if getattr(self, '_enhanced', False):
                        for item in summary['solutions']:
                            item['applicable'], item['applicability_reason'] = check_conditions(item.get('conditions', {}), event.line, self._facts, self._system)
                            try:
                                validate_solution_context(event.line, item.get('commands', []))
                            except ValueError as exc:
                                item['applicable'], item['applicability_reason'] = False, str(exc)
                    candidates = self._ui.show_history(summary, related)
                except Exception as exc:
                    self._ui.error(f'History management failed: {exc}')
                action, selected = self._ui.choose_history_action(candidates)
            if action == "ignore":
                self._ignore_error("Error ignored; occurrence recorded.")
                return
            if action == "paste":
                self._save_custom(history, key, occurrence)
                return
            if action == "reuse":
                self._selected_reference = selected
                self._note('selected_history_reference', {'solution_id': selected.get('id')})
                self._ui.info('Saved solution selected as reference. The solver will adapt it to the confirmed current diagnosis.')
            references = [knowledge_card(event.line, summary, getattr(self, '_system', {}))]
            references.extend(knowledge_card(item['error'], item['summary'], item.get('environment')) for item in related)
            self._history_references = references
            combined_diag += "\nHistorical repair references (adapt to the confirmed diagnosis):\n" + bounded_context({
                'cases': references,
                'selected_reference': selected if action == 'reuse' else None,
                'preferred_solution_id': selected.get('id') if action == 'reuse' and selected else None})

        fix_cmds = self._propose_fixes(event, combined_diag, history, key, occurrence)
        if fix_cmds is None:
            return

        try:
            validate_solution_context(event.line, fix_cmds)
        except ValueError as exc:
            self._ui.error(f"Proposed solution is not applicable: {exc}")
            return

        self._note("proposed_fixes", fix_cmds)
        manual = getattr(self, '_manual_steps', [])
        if manual:
            approved = self._ui.show_manual_solution(manual, getattr(self, '_solution_description', ''))
            if approved and fix_cmds:
                approved = self._ui.show_solution_panel(fix_cmds)
        else:
            approved = self._ui.show_solution_panel(fix_cmds)
        if not approved:
            self._ui.info("Fix skipped by user.")
            return

        execution = self._apply_fixes(fix_cmds)
        if manual:
            self._note('manual_actions', manual)
        if getattr(self, '_enhanced', False):
            self._verify_after_fix(event)
        if history is not None:
            reference = getattr(self, '_selected_reference', None)
            signature = lambda items: [(item['command'].removeprefix('sudo '), bool(item.get('sudo') or item['command'].startswith('sudo '))) for item in items]
            same_saved_action = reference and reference.get('id') and signature(reference.get('commands', [])) == signature(fix_cmds) and reference.get('manual_steps', []) == manual
            self._finish_solution(history, key, occurrence, fix_cmds, getattr(self, '_solution_description', '') or 'Agent proposed fix', getattr(self, '_proposal_source', 'agent'), execution,
                                  reference['id'] if same_saved_action else None)
        elif getattr(self, '_enhanced', False):
            outcome = self._ui.ask_outcome()
            self._session.data['outcome'] = outcome
            self._note('user_outcome', outcome)
            self._stage('Outcome saved', 'Outcome retained in this report; monitoring will continue')

    def _propose_fixes(self, event, combined_diag, history=None, key=None, occurrence=None):
        """Resolve a solver result explicitly before moving to report export."""
        rejected_proposals = 0
        while True:
            self._stage('Fix proposed', 'Review the expected effect, privileges and verification before applying a fix')
            self._proposal_source = 'agent'
            # Only a confirmed diagnosis proceeds to proposed fixes.
            solution: list[dict] | dict | None = None

            for attempt in range(2):   # at most one round of extra diagnostics
                hint = (getattr(self, '_last_verdict', None) or {}).get('repair_hint')
                if attempt == 0 and hint and getattr(self, '_last_verdict', {}).get('assessment_source') == 'local_executable_lookup':
                    solution = [hint]
                    self._proposal_source = 'local-package-rule'
                    self._solution_description = hint['description']
                    break
                reference = linux_resolution(getattr(self, '_library_plan', None), getattr(self, '_last_verdict', None) or {})
                if attempt == 0 and reference and not getattr(self, '_selected_reference', None):
                    solution = reference
                    self._proposal_source = 'bundled-linux-reference'
                    self._ui.info('Using Linux reference guidance for the confirmed current diagnosis; no LLM request needed.')
                    break
                self._ui.thinking(
                    "Solver Agent is generating fix commands…"
                    if attempt == 0
                    else "Solver Agent is re-analysing with additional data…"
                )
                try:
                    solution = self._llm(lambda: self._solver.solve(event.line, combined_diag), "Generating proposed fixes")
                except (RuntimeError, ValueError) as exc:
                    self._ui.error(f"Solver Agent failed: {exc}")
                    return

                # Model asked for more diagnostic data → run extra commands
                if isinstance(solution, dict) and solution.get("need_more_diagnostics"):
                    reason = solution.get("reason", "No reason given")
                    extra_cmds = solution.get("commands", [])
                    self._ui.info(f"Solver needs more information: {reason}")

                    if not extra_cmds:
                        break  # Report the missing information in the unresolved step

                    if attempt == 1:
                        solution = {"no_applicable_fix": True, "reason": "The solver still needs more evidence after the additional diagnostic round: " + reason}
                        break

                    try:
                        validate_diagnostic_commands(extra_cmds)
                    except ValueError as exc:
                        self._ui.error(f"Additional diagnostics rejected: {exc}")
                        return

                    allowed = self._ui.show_diagnostic_panel(
                        extra_cmds, title="Additional Diagnostics"
                    )
                    if not allowed:
                        self._ui.info("Additional diagnostics skipped.")
                        return None

                    extra_output = self._collect_diagnostics(extra_cmds, event, 'additional_diagnostic_output')
                    if getattr(self, '_enhanced', False):
                        self._checks.extend(extra_cmds)
                    combined_diag += f"\n\nAdditional diagnostics:\n{extra_output}"
                    if getattr(self, '_enhanced', False):
                        try:
                            reassessed = self._llm(lambda: self._diagnostic.verify(event.line, self._hypothesis, combined_diag,
                                executed_commands=[item['command'] for item in self._evidence_captures], separate=True,
                                captures=self._evidence_captures), 'Reassessing symptom and cause')
                        except (RuntimeError, ValueError) as exc:
                            self._ui.error('Could not reassess additional evidence: ' + str(exc))
                            return None
                        reassessed = assessment(reassessed)
                        self._note('diagnostic_conclusion', reassessed)
                        self._ui.show_diagnostic_verdict(reassessed)
                        if reassessed['symptom']['status'] != 'confirmed' or reassessed['cause']['status'] != 'confirmed':
                            action = self._ui.choose_diagnostic_next(reassessed)
                            if action == 'research':
                                fresh = self._diagnose_error(event, combined_diag + '\nReassessment: ' + json.dumps(reassessed))
                                if fresh is None:
                                    return None
                                combined_diag = fresh
                            else:
                                if action == 'ignore':
                                    self._ignore_error()
                                else:
                                    self._session.data['outcome'] = 'watching'
                                    self._note('resolution', 'Watch for recurrence after inconclusive additional evidence')
                                return None
                    continue  # Ask solver again with the richer context

                break  # Got a real fix list; exit loop

            self._manual_steps = solution.get('manual_steps', []) if isinstance(solution, dict) else []
            self._solution_description = solution.get('description', '') if isinstance(solution, dict) else ''
            if isinstance(solution, list) and hint and solution == [hint]:
                self._solution_description = hint['description']
            if isinstance(solution, dict) and solution.get('verification'):
                self._solution_description += '\nVerify original operation: ' + solution['verification']
            # Normalise solution to a list of fix-command dicts
            if isinstance(solution, list):
                fix_cmds = solution
            elif isinstance(solution, dict) and not solution.get("need_more_diagnostics"):
                fix_cmds = solution.get("commands", [])
            else:
                fix_cmds = []

            if fix_cmds:
                try:
                    validate_solution_context(event.line, fix_cmds)
                except ValueError as exc:
                    self._note('rejected_proposed_fix', {'reason': str(exc), 'commands': fix_cmds})
                    self._ui.error('Proposed solution is not applicable: ' + str(exc))
                    if not getattr(self, '_enhanced', False):
                        return None
                    if rejected_proposals == 0:
                        rejected_proposals += 1
                        combined_diag += '\nRejected previous fix: ' + str(exc) + '\nReturn a relevant evidence-based fix or explain why none is available.'
                        continue
                    solution = {'no_applicable_fix': True, 'reason': 'The solver repeated an unrelated fix. ' + str(exc)}
                else:
                    return fix_cmds
            if self._manual_steps:
                return fix_cmds
            reason = (solution.get('reason') if isinstance(solution, dict) else None) or 'The LLM returned an empty command list without a reason. Retry it, gather more evidence, or supply your own solution.'
            self._note('no_applicable_fix', {'reason': reason})
            if not getattr(self, '_enhanced', False):
                self._ui.info('No fix proposed: ' + reason)
                return None
            action = self._ui.choose_unresolved_action(reason, can_save=history is not None)
            if action == 'retry':
                continue
            if action == 'research':
                evidence = self._diagnose_error(event, combined_diag + '\nPrevious solver could not propose a fix: ' + reason)
                if evidence is None:
                    return None
                combined_diag = evidence
                continue
            if action == 'paste' and history is not None:
                self._save_custom(history, key, occurrence)
            else:
                self._note('resolution', 'User continued monitoring without a fix')
            return None

    def _diagnose_error(self, event: ErrorEvent, context: str = "") -> str | None:
        evidence = context
        use_library = True
        while True:
            self._stage('Diagnosing', 'Approve read-only checks to establish the symptom and test the suspected cause')
            try:
                plan = None
                history = getattr(self, '_history', None)
                lookup = getattr(history, 'linux_diagnostic_plan', None)
                if use_library and callable(lookup):
                    try:
                        candidate = lookup(event.line)
                        if isinstance(candidate, dict):
                            plan = candidate
                    except Exception as exc:
                        self._ui.info('Linux reference lookup unavailable; using LLM diagnostics: ' + str(exc))
                use_library = False  # Further research must not repeat the same local checks forever.
                self._library_plan = plan
                if plan:
                    self._ui.info('Local knowledge: ' + plan['library_category'] + ' · ' + plan['hypothesis'] + '. No LLM request for this diagnostic plan.')
                    self._note('linux_reference_selected', {'id': plan['library_id'], 'category': plan['library_category'], 'sources': plan['sources']})
                else:
                    self._ui.thinking("No applicable local plan; Diagnostic Agent is analysing the error…")
                    plan = self._llm(lambda: self._diagnostic.diagnose(event.line, evidence, trusted_checks=getattr(self, '_trusted_checks', [])), "Diagnosing current error")
            except (RuntimeError, ValueError) as exc:
                self._ui.error(f"Diagnostic Agent failed: {exc}")
                return None
            self._ui.info(f"Suspected problem: {plan['hypothesis']}")
            self._note('diagnostic_plan', plan)
            if getattr(self, '_enhanced', False):
                self._ui.show_structured_diagnosis(event.line, plan, file_facts(event.line))
            if not self._ui.show_diagnostic_panel(plan["commands"]):
                self._ui.info("Diagnostics skipped.")
                return None
            output = self._collect_diagnostics(plan['commands'], event, 'diagnostic_output')
            if getattr(self, '_enhanced', False):
                self._checks = plan['commands']
                self._verification_checks = plan.get('verification_checks', [])
                local = local_check_results(self._verification_checks, file_facts(event.line))
                self._note('verification_criteria', self._verification_checks)
                output += '\nMeasured pre-fix criteria: ' + json.dumps(local)
            evidence += f"\nHypothesis: {plan['hypothesis']}\n{output}\n"
            try:
                verdict = evaluate_linux(self._library_plan, getattr(self, '_captures', []), event.line)
                if verdict and verdict['status'] == 'inconclusive':
                    evidence += '\nLocal measured facts (cause still needs investigation): ' + json.dumps(verdict)
                    verdict = None
                if verdict is None:
                    self._ui.thinking("Evaluating evidence; LLM reasoning is used when local rules are insufficient…")
                    verdict = self._llm(lambda: self._diagnostic.verify(event.line, plan["hypothesis"], evidence,
                        executed_commands=[item['command'] for item in self._evidence_captures] or [item['command'] for item in plan['commands']],
                        separate=True, captures=self._evidence_captures), "Evaluating diagnostic evidence")
                else:
                    self._ui.info('Current evidence assessed locally; no LLM request needed.')
            except (RuntimeError, ValueError) as exc:
                self._ui.error(f"Diagnostic verification failed: {exc}")
                return None
            verdict = assessment(verdict)
            self._ui.show_diagnostic_verdict(verdict)
            self._note('diagnostic_conclusion', verdict)
            if getattr(self, '_enhanced', False):
                self._last_verdict = verdict
                self._hypothesis = plan['hypothesis']
            evidence += "Verdict: " + json.dumps(verdict) + "\n"
            if history := getattr(self, '_history', None):
                try:
                    playbook = history.save_playbook(event.line, plan, verdict,
                        case_id=self._session.data['id'] if getattr(self, '_session', None) else None)
                    self._note('diagnostic_scenario_saved', {'id': playbook})
                except Exception as exc:
                    self._ui.error('Could not save diagnostic scenario: ' + str(exc))
            if verdict['symptom']['status'] == verdict['cause']['status'] == 'confirmed':
                if getattr(self, '_enhanced', False):
                    self._conditions = solution_conditions(event.line, file_facts(event.line), self._system)
                return evidence
            action = self._ui.choose_diagnostic_next(verdict) if getattr(self, '_enhanced', False) else ('research' if self._ui.ask_research_further() else 'ignore')
            if action == 'research':
                continue
            if action == 'watch':
                if getattr(self, '_session', None):
                    self._session.data['outcome'] = 'watching'
                self._note('resolution', 'Watch for recurrence; the symptom was not established or its cause remains unknown')
                return None
            self._ignore_error()
            return None

    def _verify_after_fix(self, event):
        self._stage('Verification', 'Rerun approved checks to determine whether the original problem still exists')
        if not self._checks:
            self._note('after_fix_verification', 'No approved read-only checks available')
            self._ui.info('Automatic verification unavailable; user confirmation is required.')
            return
        validate_diagnostic_commands(self._checks)
        if not self._ui.show_diagnostic_panel(self._checks, title='Verify the fix (rerun read-only checks)'):
            self._note('after_fix_verification', 'User skipped verification')
            return
        output = self._collect_diagnostics(self._checks, event, 'after_fix_output')
        current = file_facts(event.line)
        evidence = 'AFTER FIX: Determine if the ORIGINAL problem still exists. Confirmed means it persists; disproved means evidence shows it resolved; inconclusive means unknown.\n' + json.dumps({'current_files': current}) + '\n' + output
        checks = local_check_results(getattr(self, '_verification_checks', []), current)
        self._note('local_verification', checks)
        evidence += '\nMeasured criteria: ' + json.dumps(checks)
        try:
            verdict = evaluate_linux(getattr(self, '_library_plan', None), self._captures, event.line, after_fix=True)
            if verdict is None or verdict['status'] == 'inconclusive':
                verdict = self._llm(lambda: self._diagnostic.verify(event.line, getattr(self, '_hypothesis', event.line), evidence, executed_commands=[item['command'] for item in self._checks], separate=True, after_fix=True, captures=self._captures), 'Verifying the applied fix')
        except (RequestCancelled, RuntimeError, ValueError) as exc:
            self._note('after_fix_verification_error', str(exc))
            self._note('after_fix_verification', 'User cancelled verification')
            self._ui.info('Verification cancelled; please report the outcome yourself.')
            return
        if checks and any(item['state'] != 'expected' for item in checks) and verdict['status'] == 'disproved':
            verdict = dict(verdict, status='inconclusive', explanation='Required measured criteria are not all satisfied. ' + verdict['explanation'],
                           symptom={'status':'inconclusive', 'explanation':'Measured verification criteria are not all satisfied'})
        self._ui.show_verification(verdict)
        self._note('after_fix_conclusion', verdict)

    def _record_attempt(self, history, key, occurrence, solution_id, outcome, execution=None):
        session = getattr(self, '_session', None)
        id_ = getattr(self, '_attempt_id', None) if execution is not None else None
        history.record_attempt(key, occurrence, solution_id, outcome, execution,
            case_id=session.data['id'] if session else None,
            verification=session.data.get('verification') if session and execution is not None else None,
            attempt_id=id_, occurrence_ids=getattr(self, '_active_occurrences', None) or [occurrence])
        if session and id_:
            session.data['attempt_id'] = id_

    def _record_application_progress(self, execution):
        session = getattr(self, '_session', None)
        history = getattr(self, '_history', None)
        if session and history and session.data.get('error_key'):
            try:
                self._record_attempt(history, session.data['error_key'], session.data['occurrence_ids'][0], None, 'not_reported', execution)
            except Exception as exc:
                self._ui.error('Could not persist application progress; details remain in the current case: ' + str(exc))

    def _save_custom(self, history, key, occurrence) -> None:
        commands, notes = self._ui.read_custom_solution()
        if not commands and not notes.strip():
            return
        outcome = self._ui.ask_outcome()
        self._note('custom_solution', {'notes': notes, 'commands': commands, 'outcome': outcome})
        if getattr(self, '_session', None) is not None:
            self._session.data['outcome'] = outcome
            self._session.data['feedback_scope'] = 'custom_solution'
        try:
            solution_id = history.save_solution(key, commands, notes, "user")
            self._record_attempt(history, key, occurrence, solution_id, outcome)
            self._note('saved_solution', {'solution_id': solution_id, 'feedback': outcome})
            self._stage('Outcome saved', 'Review the result; monitoring will continue')
            self._ui.info("Custom solution saved. No commands executed.")
        except Exception as exc:
            self._ui.error(f"Could not save solution history: {exc}")

    def _finish_solution(self, history, key, occurrence, commands, notes, source, execution, saved_id=None) -> None:
        self._stage('Verification', 'Report the result separately from deciding whether to save this solution')
        outcome = self._ui.ask_outcome()
        self._note('user_outcome', outcome)
        if getattr(self, '_session', None) is not None:
            self._session.data['outcome'] = outcome
        if getattr(self, '_attempt_id', None):
            try:
                self._record_attempt(history, key, occurrence, saved_id, outcome, execution)
            except Exception as exc:
                self._ui.error('Could not persist outcome before saving the solution: ' + str(exc))
        choice = self._ui.ask_save_solution()
        try:
            solution_id = saved_id
            if choice == "save":
                solution_id = saved_id or history.save_solution(key, commands, notes, source,
                    **({'conditions': self._conditions, 'verification_commands': self._checks,
                        'diagnosis': {'cause': self._session.data.get('cause_assessment'), 'category': self._conditions.get('problem_kind')},
                        'manual_steps': getattr(self, '_manual_steps', []), 'verification_checks': getattr(self, '_verification_checks', [])} if getattr(self, '_enhanced', False) else {}))
            elif choice == "paste":
                # The pasted alternative may have a different outcome from the
                # commands just executed, so ask and record it separately.
                self._record_attempt(history, key, occurrence, saved_id, outcome, execution)
                if getattr(self, '_session', None):
                    self._session.data['verification_solution_id'] = saved_id
                self._save_custom(history, key, occurrence)
                return
            self._record_attempt(history, key, occurrence, solution_id, outcome, execution)
            self._stage('Outcome saved', 'Review the result; monitoring will continue')
            self._note('saved_solution', {'solution_id': solution_id, 'feedback': outcome,
                'automatically_checked': bool(getattr(self, '_session', None) and self._session.data.get('verification'))})
            self._ui.info(('Feedback skipped.' if outcome == 'skipped' else 'Outcome recorded.') + (" Solution saved." if solution_id else " Solution not saved."))
        except Exception as exc:
            self._ui.error(f"Could not save solution history: {exc}")

    def _apply_fixes(self, commands: list[dict]) -> list[dict]:
        """Execute fix commands one by one, displaying success/failure after each."""
        self._ui.info(f"Applying {len(commands)} fix command(s)…")
        execution = []
        if getattr(self, '_enhanced', False):
            self._attempt_id = str(uuid4())
            if getattr(self, '_session', None):
                self._session.data['attempt_id'] = self._attempt_id
                self._note('attempt_started', {'id': self._attempt_id, 'commands': commands})
        for cmd_info in commands:
            # Extract the raw command string
            cmd_str = cmd_info["command"]
            
            # If the solver flagged this as requiring sudo, prepend it
            if cmd_info.get("sudo") and not cmd_str.startswith('sudo '):
                cmd_str = f"sudo {cmd_str}"
                
            success, output = run_command(
                cmd_str, timeout=config.COMMAND_TIMEOUT
            )
            
            # Display the result using the executed command string
            self._ui.show_fix_result(cmd_str, success, output)
            execution.append(redact({'command': cmd_str, 'success': success, 'output': output}))
            self._note('fix_result', {'command': cmd_str, 'success': success, 'output': output})
            self._record_application_progress(execution)
            if not success:
                self._ui.info('Stopped remaining fix commands after a failed command.')
                break
            
        self._record_application_progress(execution)
        self._ui.info('Actions completed; resolution still requires verification.')
        return execution

    @staticmethod
    def _load_paths(config_file: str) -> list[str]:
        """
        Parse *config_file* (``logs.lst``) and return a list of paths.

        Lines that are blank or start with ``#`` are ignored.

        Raises
        ------
        FileNotFoundError
            If the config file is missing or contains no valid paths.
        """
        paths: list[str] = []
        try:
            with open(config_file, "r") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        path = Path(stripped).expanduser()
                        if not path.is_absolute():
                            path = Path(config_file).resolve().parent / path
                        paths.append(str(path.resolve()))
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"Config file '{config_file}' not found. "
                f"Create it with one log path per line."
            ) from exc

        if not paths:
            raise FileNotFoundError(
                f"No valid paths found in '{config_file}'. "
                f"Add at least one absolute file or directory path."
            )
        return paths
