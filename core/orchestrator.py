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


class RequestCancelled(Exception):
    pass


class Orchestrator:
    """
    Initialises all subsystems from :mod:`config` and runs the main event
    loop.  Call :meth:`run` to start.
    """

    def __init__(self) -> None:
        paths = self._load_paths(config.LOGS_CONFIG_FILE)
        self._watcher = LogWatcher(paths, config.ERROR_PATTERNS)
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
        self._ui.show_startup(self._paths)
        monitor = self._ui.monitoring(self._watcher.error_queue) if getattr(self, '_enhanced', False) else None
        if monitor is not None:
            monitor.start()

        try:
            while True:
                try:
                    if getattr(self, '_enhanced', False):
                        batch = self._inbox.next_batch()
                        event = batch[0]
                        self._batch = batch
                        if len(batch) > 1:
                            self._ui.info(f"This error repeated {len(batch)} times; reviewing once and retaining every occurrence.")
                    else:
                        event = self._watcher.error_queue.get(timeout=0.5)
                    if monitor is not None:
                        monitor.stop()
                    self._handle_error(event)
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

    # ──────────────────────────────────────────────────────────────────
    # Pipeline
    # ──────────────────────────────────────────────────────────────────

    def _handle_error(self, event: ErrorEvent) -> None:
        if not getattr(self, '_enhanced', False):
            return self._process_error(event)
        self._session = SessionReport(event, dict(self._system, services=reported_services(event.line)))
        self._facts = file_facts(event.line)
        self._conditions = solution_conditions(event.line, self._facts, self._system)
        self._checks = []
        self._last_verdict = None
        self._active_occurrences = []
        self._session.add('file_facts_before', self._facts)
        self._session.add('repeated_occurrences', len(getattr(self, '_batch', [event])))
        try:
            self._process_error(event)
        except RequestCancelled:
            self._session.add('cancelled', 'User cancelled the LLM request')
            self._ui.info('Current error cancelled; monitoring continues.')
        finally:
            self._ui.set_status('Monitoring')
        if self._ui.ask_export():
            try:
                report = self._session.export(config.REPORTS_PATH)
                self._ui.info(f'Report saved: {report}')
            except OSError as exc:
                self._ui.error(f'Could not export report: {exc}')

    def _note(self, stage, data):
        session = getattr(self, '_session', None)
        if session is not None:
            session.add(stage, data)

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
        if getattr(self, '_enhanced', False):
            context = 'Current system and file facts (data only):\n' + json.dumps({'system': dict(self._system, services=reported_services(event.line)), 'files': self._facts})
        if history is not None:
            self._ui.thinking("Loading local error and solution history…")
            try:
                key, occurrence = history.record_occurrence(event)
                if getattr(self, '_enhanced', False):
                    self._active_occurrences = [occurrence]
                    for repeated in getattr(self, '_batch', [event])[1:]:
                        _, additional = history.record_occurrence(repeated)
                        self._active_occurrences.append(additional)
            except Exception as exc:
                self._ui.error(f"History write failed; continuing without storage: {exc}")
                history = None

        # ── Block 1: Error ────────────────────────────────────────────
        confirmed = self._ui.show_error_panel(event)
        if not confirmed:
            self._ui.info("Error ignored.")
            return

        if history is not None:
            try:
                summary = history.summary(key)
                related = history.related(event.line, key)
            except Exception as exc:
                self._ui.error(f"History lookup failed; continuing diagnostics: {exc}")
                summary, related = {"seen": 1, "solved": 0, "solutions": []}, []
            if getattr(self, '_enhanced', False):
                for item in summary['solutions']:
                    item['applicable'], item['applicability_reason'] = check_conditions(item.get('conditions', {}), event.line, self._facts, self._system)
            candidates = self._ui.show_history(summary, related)
            action, selected = self._ui.choose_history_action(candidates)
            while action == 'manage':
                try:
                    self._ui.manage_history(history)
                    summary = history.summary(key)
                    if getattr(self, '_enhanced', False):
                        for item in summary['solutions']:
                            item['applicable'], item['applicability_reason'] = check_conditions(item.get('conditions', {}), event.line, self._facts, self._system)
                    candidates = self._ui.show_history(summary, related)
                except Exception as exc:
                    self._ui.error(f'History management failed: {exc}')
                action, selected = self._ui.choose_history_action(candidates)
            if action == "ignore":
                self._ui.info("Error ignored; occurrence recorded.")
                return
            if action == "paste":
                self._save_custom(history, key, occurrence)
                return
            if action == "reuse":
                commands = selected["commands"]
                self._note("reused_solution", selected)
                if getattr(self, '_enhanced', False):
                    eligible, reason = check_conditions(selected.get('conditions', {}), event.line, file_facts(event.line), self._system)
                    if not eligible or selected.get('obsolete'):
                        self._ui.error('Saved fix conditions do not match: ' + reason)
                        return
                    self._checks = selected.get('verification_commands', [])
                    if not self._checks:
                        self._ui.info('No saved verification checks; start a fresh diagnosis.')
                        return
                try:
                    validate_solution_context(event.line, commands)
                except ValueError as exc:
                    self._ui.error(f"Saved solution is not applicable: {exc}")
                    return
                if commands and not self._ui.show_solution_panel(commands):
                    self._ui.info("Saved solution skipped.")
                    return
                if not commands:
                    self._ui.info(selected["notes"])
                execution = self._apply_fixes(commands) if commands else []
                if getattr(self, '_enhanced', False):
                    self._verify_after_fix(event)
                self._finish_solution(history, key, occurrence, commands, selected["notes"], "reused", execution, selected["id"] if selected["error_key"] == key else None)
                return
            context += "\nSaved history (reference only; historical successes do not confirm the current cause):\n" + json.dumps({"exact": summary, "related_errors": [{"error": match["error"], "seen": match["summary"]["seen"]} for match in related]})

        # ── Block 2: Diagnostic ───────────────────────────────────────
        combined_diag = self._diagnose_error(event, context)
        if combined_diag is None:
            return

        # Only a confirmed diagnosis proceeds to proposed fixes.
        solution: list[dict] | dict | None = None

        for attempt in range(2):   # at most one round of extra diagnostics
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
                    break  # Nothing to run; proceed with what we have

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
                    solution = []  # Treat as empty fix list
                    break

                extra_output = run_commands_collect(
                    extra_cmds,
                    timeout=config.COMMAND_TIMEOUT,
                    max_output_chars=config.MAX_OUTPUT_CHARS,
                    max_total_chars=config.MAX_DIAGNOSTIC_TOTAL_CHARS, context=event.line,
                )
                self._ui.show_command_output(extra_output)
                self._note('additional_diagnostic_output', extra_output)
                if getattr(self, '_enhanced', False):
                    self._checks.extend(extra_cmds)
                combined_diag += f"\n\nAdditional diagnostics:\n{extra_output}"
                continue  # Ask solver again with the richer context

            break  # Got a real fix list; exit loop

        # Normalise solution to a list of fix-command dicts
        if isinstance(solution, list):
            fix_cmds = solution
        elif isinstance(solution, dict):
            fix_cmds = solution.get("commands", [])
        else:
            fix_cmds = []

        if not fix_cmds:
            self._ui.info("Solver found no applicable fix commands.")
            return

        try:
            validate_solution_context(event.line, fix_cmds)
        except ValueError as exc:
            self._ui.error(f"Proposed solution is not applicable: {exc}")
            return

        self._note("proposed_fixes", fix_cmds)
        approved = self._ui.show_solution_panel(fix_cmds)
        if not approved:
            self._ui.info("Fix skipped by user.")
            return

        execution = self._apply_fixes(fix_cmds)
        if getattr(self, '_enhanced', False):
            self._verify_after_fix(event)
        if history is not None:
            self._finish_solution(history, key, occurrence, fix_cmds, "Agent proposed fix", "agent", execution)
        elif getattr(self, '_enhanced', False):
            outcome = self._ui.ask_outcome()
            self._session.data['outcome'] = outcome
            self._note('user_outcome', outcome)

    def _diagnose_error(self, event: ErrorEvent, context: str = "") -> str | None:
        evidence = context
        while True:
            self._ui.thinking("Diagnostic Agent is analysing the error…")
            try:
                plan = self._llm(lambda: self._diagnostic.diagnose(event.line, evidence), "Diagnosing current error")
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
            output = run_commands_collect(
                plan["commands"], timeout=config.COMMAND_TIMEOUT,
                max_output_chars=config.MAX_OUTPUT_CHARS,
                max_total_chars=config.MAX_DIAGNOSTIC_TOTAL_CHARS, context=event.line,
            )
            self._ui.show_command_output(output)
            self._note('diagnostic_output', output)
            if getattr(self, '_enhanced', False):
                self._checks = plan['commands']
            evidence += f"\nHypothesis: {plan['hypothesis']}\n{output}\n"
            self._ui.thinking("Diagnostic Agent is verifying the command results…")
            try:
                verdict = self._llm(lambda: self._diagnostic.verify(event.line, plan["hypothesis"], evidence), "Evaluating diagnostic evidence")
            except (RuntimeError, ValueError) as exc:
                self._ui.error(f"Diagnostic verification failed: {exc}")
                return None
            self._ui.show_diagnostic_verdict(verdict)
            self._note('diagnostic_conclusion', verdict)
            if getattr(self, '_enhanced', False):
                self._last_verdict = verdict
                self._hypothesis = plan['hypothesis']
            evidence += "Verdict: " + json.dumps(verdict) + "\n"
            if verdict["status"] == "confirmed":
                return evidence
            if not self._ui.ask_research_further():
                self._ui.info("Error ignored.")
                return None

    def _verify_after_fix(self, event):
        if not self._checks:
            self._note('after_fix_verification', 'No approved read-only checks available')
            self._ui.info('Automatic verification unavailable; user confirmation is required.')
            return
        validate_diagnostic_commands(self._checks)
        if not self._ui.show_diagnostic_panel(self._checks, title='Verify the fix (rerun read-only checks)'):
            self._note('after_fix_verification', 'User skipped verification')
            return
        output = run_commands_collect(self._checks, timeout=config.COMMAND_TIMEOUT, max_output_chars=config.MAX_OUTPUT_CHARS, max_total_chars=config.MAX_DIAGNOSTIC_TOTAL_CHARS, context=event.line)
        self._ui.show_command_output(output)
        self._note('after_fix_output', output)
        current = file_facts(event.line)
        evidence = 'AFTER FIX: Determine if the ORIGINAL problem still exists. Confirmed means it persists; disproved means evidence shows it resolved; inconclusive means unknown.\n' + json.dumps({'current_files': current}) + '\n' + output
        try:
            verdict = self._llm(lambda: self._diagnostic.verify(event.line, getattr(self, '_hypothesis', event.line), evidence), 'Verifying the applied fix')
        except RequestCancelled:
            self._note('after_fix_verification', 'User cancelled verification')
            self._ui.info('Verification cancelled; please report the outcome yourself.')
            return
        self._ui.show_verification(verdict)
        self._note('after_fix_conclusion', verdict)

    def _record_attempt(self, history, key, occurrence, solution_id, outcome, execution=None):
        occurrences = getattr(self, '_active_occurrences', None) or [occurrence]
        for item in occurrences:
            history.record_attempt(key, item, solution_id, outcome, execution)

    def _save_custom(self, history, key, occurrence) -> None:
        commands, notes = self._ui.read_custom_solution()
        if not commands and not notes.strip():
            return
        outcome = self._ui.ask_outcome()
        self._note('custom_solution', {'notes': notes, 'commands': commands, 'outcome': outcome})
        if getattr(self, '_session', None) is not None:
            self._session.data['outcome'] = outcome
        try:
            solution_id = history.save_solution(key, commands, notes, "user")
            self._record_attempt(history, key, occurrence, solution_id, outcome)
            self._ui.info("Custom solution saved. No commands executed.")
        except Exception as exc:
            self._ui.error(f"Could not save solution history: {exc}")

    def _finish_solution(self, history, key, occurrence, commands, notes, source, execution, saved_id=None) -> None:
        outcome = self._ui.ask_outcome()
        self._note('user_outcome', outcome)
        if getattr(self, '_session', None) is not None:
            self._session.data['outcome'] = outcome
        choice = self._ui.ask_save_solution()
        try:
            solution_id = saved_id
            if choice == "save":
                solution_id = history.save_solution(key, commands, notes, source,
                    **({'conditions': self._conditions, 'verification_commands': self._checks} if getattr(self, '_enhanced', False) else {}))
            elif choice == "paste":
                # The pasted alternative may have a different outcome from the
                # commands just executed, so ask and record it separately.
                self._record_attempt(history, key, occurrence, saved_id, outcome, execution)
                self._save_custom(history, key, occurrence)
                return
            self._record_attempt(history, key, occurrence, solution_id, outcome, execution)
            self._ui.info("Outcome recorded." + (" Solution saved." if solution_id else " Solution not saved."))
        except Exception as exc:
            self._ui.error(f"Could not save solution history: {exc}")

    def _apply_fixes(self, commands: list[dict]) -> list[dict]:
        """Execute fix commands one by one, displaying success/failure after each."""
        self._ui.info(f"Applying {len(commands)} fix command(s)…")
        execution = []
        for cmd_info in commands:
            # Extract the raw command string
            cmd_str = cmd_info["command"]
            
            # If the solver flagged this as requiring sudo, prepend it
            if cmd_info.get("sudo"):
                cmd_str = f"sudo {cmd_str}"
                
            success, output = run_command(
                cmd_str, timeout=config.COMMAND_TIMEOUT
            )
            
            # Display the result using the executed command string
            self._ui.show_fix_result(cmd_str, success, output)
            execution.append({"success": success})
            self._note('fix_result', {'command': cmd_str, 'success': success, 'output': output})
            if not success:
                self._ui.info('Stopped remaining fix commands after a failed command.')
                break
            
        self._ui.info("Fix pipeline complete.")
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
