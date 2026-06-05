"""
core/orchestrator.py
====================
Pipeline coordinator that manages the flow between Diagnostic Agent and
Solver Agent. Coordinates command execution and UI interactions.
"""  
from __future__ import annotations  
import logging
from core.executor import run_command, run_commands_collect
from agents.diagnostic import DiagnosticAgent
from agents.solver import SolverAgent
from ui.display import Display
import config

_logger = logging.getLogger("log_monitor.orchestrator")

class Orchestrator:
    """Coordinates the log error handling pipeline."""
    
    def __init__(self) -> None:
        self._ui = Display()
        self._diagnostic = DiagnosticAgent()
        self._solver = SolverAgent()
        _logger.info("Orchestrator initialized with agents")
    
    def run(self) -> None:
        """Main loop: wait for errors and process them."""
        _logger.info("Starting orchestrator main loop")
        while True:
            event = self._ui.get_error_event()
            if not event:
                continue
            
            _logger.info(f"Processing error: {event['line'][:50]} from {event['filepath']}")
            if self._process_error(event):
                _logger.info("Error processing complete")
    
    def _process_error(self, event: dict) -> bool:
        """
        Process a single error event through the pipeline.
        Returns True if event was handled.
        """
        # Block 1: Show error and ask for confirmation
        if not self._ui.show_error_panel(event):
            _logger.info("Error ignored by user")
            return False
        
        # Block 2: Run diagnostic commands
        self._ui.step_status("Diagnostic Agent: analyzing error")
        try:
            _logger.debug(f"{self._diagnostic.role} analyzing error")
            self._ui.thinking(f"{self._diagnostic.role}: analysing")
            diag_cmds = self._diagnostic.analyze(event["line"])
            self._ui.show_diagnostics_panel(diag_cmds)
        except (RuntimeError, ValueError) as exc:
            self._ui.error(f"Diagnostic Agent failed: {exc}")
            return False
        
        if not diag_cmds:
            self._ui.info("No diagnostic commands generated.")
            return False
        
        allowed = self._ui.show_diagnostic_panel(diag_cmds)
        if not allowed:
            self._ui.info("Diagnostic commands skipped.")
            return False
        
        self._ui.step_status("Running diagnostic commands")
        _logger.debug(f"Executing {len(diag_cmds)} diagnostic commands")
        diag_output = run_commands_collect(
            diag_cmds,
            timeout=config.COMMAND_TIMEOUT,
            max_output_chars=config.MAX_OUTPUT_CHARS,
        )
        self._ui.show_command_output(diag_output)
        self._ui.info("Diagnostics complete")
        
        # Block 3: Generate and apply fixes (with optional re-diagnosis)
        combined_diag = diag_output
        fix_cmds = []
        attempt = 0
        
        while attempt < 2:  # at most one round of extra diagnostics
            self._ui.step_status(f"Solving error (attempt {attempt + 1})")
            try:
                _logger.debug(f"{self._solver.role} solving error")
                self._ui.thinking(f"{self._solver.role}: generating")
                solution = self._solver.solve(event["line"], combined_diag)
                
                if isinstance(solution, dict) and solution.get("need_more_diagnostics"):
                    reason = solution.get("reason", "No reason given")
                    extra_cmds = solution.get("commands", [])
                    _logger.info(f"Solver requesting more data: {reason}")
                    
                    if not extra_cmds:
                        break
                    
                    allowed = self._ui.show_diagnostic_panel(
                        extra_cmds, title="Additional Diagnostics"
                    )
                    if not allowed:
                        _logger.info("Additional diagnostics skipped.")
                        break
                    
                    extra_output = run_commands_collect(
                        extra_cmds,
                        timeout=config.COMMAND_TIMEOUT,
                        max_output_chars=config.MAX_OUTPUT_CHARS,
                    )
                    self._ui.show_command_output(extra_output)
                    combined_diag += f"\n\nAdditional diagnostics:\n{extra_output}"
                    attempt += 1
                    continue
                
                if isinstance(solution, list):
                    fix_cmds = solution
                else:
                    fix_cmds = solution.get("commands", [])
                break  # Got a real fix list; exit loop
            except (RuntimeError, ValueError) as exc:
                self._ui.error(f"Solver Agent failed: {exc}")
                return False
        
        if not fix_cmds:
            self._ui.info("Solver found no applicable fix commands.")
            return False
        
        self._ui.show_solution_panel(fix_cmds)
        self._ui.step_status("Applying fix commands")
        self._apply_fixes(fix_cmds)
        
        return True
    
    def _apply_fixes(self, commands: list[dict]) -> None:
        """Execute fix commands one by one, displaying success/failure after each."""
        _logger.info(f"Applying {len(commands)} fix command(s)")
        for cmd in commands:
            self._ui.show_fix_result(cmd["command"], *run_command(
                cmd["command"], timeout=config.COMMAND_TIMEOUT
            ))
        self._ui.info("Fix pipeline complete.")
