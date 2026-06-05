"""
agents/solver.py
================
SolverAgent  
Receives the original error text plus the combined output of all diagnostic
commands, then returns either:  
A) A list of fix commands, each annotated with a criticality level
("safe" / "warning" / "danger") for colour-coded display in the UI.  
B) A special dict asking for additional diagnostics, when the available
information is insufficient to propose a safe fix.
"""  
from __future__ import annotations  
from .base import Agent
import logging

_logger = logging.getLogger("log_monitor.solver")

_SYSTEM_PROMPT = """\
You are a senior Linux system administrator and automated problem solver.  
Given an original log error and the output of diagnostic commands, produce \
fix commands.  
━━━ RESPONSE FORMAT A  (normal case) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Raw JSON array only — no markdown, no preamble:
[
{
"command":     "chmod +x /path/to/script.sh",
"description": "Grant execute permission to the script",
"criticality": "warning"
}
]  
━━━ RESPONSE FORMAT B  (need more data) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Use ONLY when truly insufficient data to suggest a safe fix:
{
"need_more_diagnostics": true,
"reason": "Need to know the file owner before changing permissions",
"commands": [
{"command": "stat /path/to/file", "description": "Get detailed file metadata"}
]
}  
━━━ CRITICALITY LEVELS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Use EXACTLY one of these strings:
- "safe"      : No risk, information gathering or benign changes
- "warning"   : Some risk, review carefully before applying
- "danger"    : High risk, destructive or system-modifying operations
"""

class SolverAgent(Agent):
    """Agent that generates fix commands for log errors."""
    
    def __init__(self):
        super().__init__(
            role="Solver Agent",
            system_prompt=_SYSTEM_PROMPT,
            model=config.OLLAMA_MODEL,
            base_url=config.OLLAMA_HOST,
            timeout=config.LLM_TIMEOUT
        )
    
    def solve(self, error_line: str, diagnostic_output: str = "") -> list[dict] | dict:
        """
        Analyze error + diagnostics and return fix commands or request more data.
        Returns either:
        - list of fix command dicts
        - dict with "need_more_diagnostics" = True for additional data request
        """
        _logger.debug(f"{self.role} solving error: {error_line[:50]}")
        prompt = f"""Original error:
{error_line}

Diagnostic output:
{diagnostic_output}

Propose fix commands."""
        raw = self.call(prompt)
        
        try:
            result = self.parse_json(raw)
        except ValueError as exc:
            _logger.error(f"{self.role} parse error: {exc}")
            raise ValueError(f"SolverAgent: {exc}") from exc
        
        # Normalise list of fix commands
        if isinstance(result, list):
            return self._normalise_fix_commands(result)
        elif isinstance(result, dict) and result.get("need_more_diagnostics"):
            _logger.info(f"{self.role} requesting additional diagnostics")
            return result
        else:
            raise ValueError(f"SolverAgent unexpected response type: {type(result)}")
    
    def _normalise_fix_commands(self, commands: list) -> list[dict]:
        """Ensure all fix commands have required keys."""
        for cmd in commands:
            if "command" not in cmd:
                cmd["command"] = ""
            if "description" not in cmd:
                cmd["description"] = "No description"
            if "criticality" not in cmd:
                cmd["criticality"] = "warning"
        return commands
