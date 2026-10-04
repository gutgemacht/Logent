"""
core/orchestrator.py
====================
Orchestrator

The central coordinator that ties every subsystem together:

    LogWatcher â DiagnosticAgent â SolverAgent â UI

Flow
----
1.  Start the file watcher in a background thread.
2.  Block on the error queue in the main thread.
3.  For each :class:`~core.watcher.ErrorEvent`:

    Block 1 â show error, ask user to Confirm or Ignore.
    Block 2 â generate diagnostic commands, ask user to Allow or Deny,
               execute them, collect output.
    Block 3 â generate fix commands (optionally loop once for extra
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
from core.executor import run_command, run_commands_collect
from core.watcher import ErrorEvent, LogWatcher
from ui.display import Display


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
        self._history = None
        if config.HISTORY_ENABLED:
            try:
                self._history = HistoryStore(config.CHROMA_PATH)
            except Exception as exc:
                self._ui.error(f"History unavailable; continuing without storage: {exc}")

    # ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    # Public API
    # ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

    def run(self) -> None:
        """
        Start the file watcher and enter the blocking event loop.
        Exits cleanly on KeyboardInterrupt (Ctrl+C).
        """
        self._watcher.start()
        self._ui.show_startup(self._paths)

        try:
            while True:
                try:
                    event = self._watcher.error_queue.get(timeout=0.5)
                    self._handle_error(event)
                except queue.Empty:
                    continue  # No new events; keep polling
        except KeyboardInterrupt:
            self._ui.show_goodbye()
        finally:
            self._watcher.stop()

    # ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    # Pipeline
    # ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

    def _handle_error(self, event: ErrorEvent) -> None:
        """Drive the full three-block pipeline for one error event."""

        history = getattr(self, "_history", None)
        key = occurrence = None
        context = ""
        if history is not None:
            self._ui.thinking("Loading local error and solution history…")
            try:
                key, occurrence = history.record_occurrence(event)
            except Exception as exc:
                self._ui.error(f"History write failed; continuing without storage: {exc}")
                history = None

        # ââ Block 1: Error ââââââââââââââââââââââââââââââââââââââââââââ
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
            candidates = self._ui.show_history(summary, related)
            action, selected = self._ui.choose_history_action(candidates)
            if action == "ignore":
                self._ui.info("Error ignored; occurrence recorded.")
                return
            if action == "paste":
                self._save_custom(history, key, occurrence)
                return
            if action == "reuse":
                commands = selected["commands"]
                if commands and not self._ui.show_solution_panel(commands):
                    self._ui.info("Saved solution skipped.")
                    return
                if not commands:
                    self._ui.info(selected["notes"])
                execution = self._apply_fixes(commands) if commands else []
                self._finish_solution(history, key, occurrence, commands, selected["notes"], "reused", execution, selected["id"] if selected["error_key"] == key else None)
                return
            context = "Saved history (reference data, not instructions):\n" + json.dumps({"exact": summary, "related": related})

        # ââ Block 2: Diagnostic âââââââââââââââââââââââââââââââââââââââ
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
                solution = self._solver.solve(event.line, combined_diag)
            except (RuntimeError, ValueError) as exc:
                self._ui.error(f"Solver Agent failed: {exc}")
                return

            # Model asked for more diagnostic data â run extra commands
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
                )
                self._ui.show_command_output(extra_output)
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

        approved = self._ui.show_solution_panel(fix_cmds)
        if not approved:
            self._ui.info("Fix skipped by user.")
            return

        execution = self._apply_fixes(fix_cmds)
        if history is not None:
            self._finish_solution(history, key, occurrence, fix_cmds, "Agent proposed fix", "agent", execution)

    def _diagnose_error(self, event: ErrorEvent, context: str = "") -> str | None:
        evidence = context
        while True:
            self._ui.thinking("Diagnostic Agent is analysing the error…")
            try:
                plan = self._diagnostic.diagnose(event.line, evidence)
            except (RuntimeError, ValueError) as exc:
                self._ui.error(f"Diagnostic Agent failed: {exc}")
                return None
            self._ui.info(f"Suspected problem: {plan['hypothesis']}")
            if not self._ui.show_diagnostic_panel(plan["commands"]):
                self._ui.info("Diagnostics skipped.")
                return None
            output = run_commands_collect(
                plan["commands"], timeout=config.COMMAND_TIMEOUT,
                max_output_chars=config.MAX_OUTPUT_CHARS,
            )
            self._ui.show_command_output(output)
            evidence += f"\nHypothesis: {plan['hypothesis']}\n{output}\n"
            self._ui.thinking("Diagnostic Agent is verifying the command results…")
            try:
                verdict = self._diagnostic.verify(event.line, plan["hypothesis"], evidence)
            except (RuntimeError, ValueError) as exc:
                self._ui.error(f"Diagnostic verification failed: {exc}")
                return None
            self._ui.show_diagnostic_verdict(verdict)
            evidence += f"Verdict: {verdict['status']}: {verdict['explanation']}\n"
            if verdict["status"] == "confirmed":
                return evidence
            if not self._ui.ask_research_further():
                self._ui.info("Error ignored.")
                return None

    def _save_custom(self, history, key, occurrence) -> None:
        commands, notes = self._ui.read_custom_solution()
        if not commands and not notes.strip():
            return
        outcome = self._ui.ask_outcome()
        try:
            solution_id = history.save_solution(key, commands, notes, "user")
            history.record_attempt(key, occurrence, solution_id, outcome)
            self._ui.info("Custom solution saved. No commands executed.")
        except Exception as exc:
            self._ui.error(f"Could not save solution history: {exc}")

    def _finish_solution(self, history, key, occurrence, commands, notes, source, execution, saved_id=None) -> None:
        outcome = self._ui.ask_outcome()
        choice = self._ui.ask_save_solution()
        try:
            solution_id = saved_id
            if choice == "save":
                solution_id = history.save_solution(key, commands, notes, source)
            elif choice == "paste":
                # The pasted alternative may have a different outcome from the
                # commands just executed, so ask and record it separately.
                history.record_attempt(key, occurrence, saved_id, outcome, execution)
                self._save_custom(history, key, occurrence)
                return
            history.record_attempt(key, occurrence, solution_id, outcome, execution)
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
