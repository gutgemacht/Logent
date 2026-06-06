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
from pathlib import Path

import config
from agents.diagnostic import DiagnosticAgent
from agents.solver import SolverAgent
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

        # ââ Block 1: Error ââââââââââââââââââââââââââââââââââââââââââââ
        confirmed = self._ui.show_error_panel(event)
        if not confirmed:
            self._ui.info("Error ignored.")
            return

        # ââ Block 2: Diagnostic âââââââââââââââââââââââââââââââââââââââ
        self._ui.thinking("Diagnostic Agent is analysing the errorâ¦")
        try:
            diag_cmds = self._diagnostic.analyze(event.line)
        except (RuntimeError, ValueError) as exc:
            self._ui.error(f"Diagnostic Agent failed: {exc}")
            return

        allowed = self._ui.show_diagnostic_panel(diag_cmds)
        if not allowed:
            self._ui.info("Diagnostics skipped.")
            return

        diag_output = run_commands_collect(
            diag_cmds,
            timeout=config.COMMAND_TIMEOUT,
            max_output_chars=config.MAX_OUTPUT_CHARS,
        )
        self._ui.show_command_output(diag_output)

        # ââ Block 3: Solution (with optional extra-diagnostics loop) ââ
        combined_diag = diag_output
        solution: list[dict] | dict | None = None

        for attempt in range(2):   # at most one round of extra diagnostics
            self._ui.thinking(
                "Solver Agent is generating fix commandsâ¦"
                if attempt == 0
                else "Solver Agent is re-analysing with additional dataâ¦"
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

        self._apply_fixes(fix_cmds)

    def _apply_fixes(self, commands: list[dict]) -> None:
        """Execute fix commands one by one, displaying success/failure after each."""
        self._ui.info(f"Applying {len(commands)} fix command(s)â¦")
        for cmd in commands:
            success, output = run_command(
                cmd["command"], timeout=config.COMMAND_TIMEOUT
            )
            self._ui.show_fix_result(cmd["command"], success, output)
        self._ui.info("Fix pipeline complete.")

    # ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    # Setup helpers
    # ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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
                        paths.append(stripped)
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
