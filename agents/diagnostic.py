"""
agents/diagnostic.py
====================
DiagnosticAgent  
Receives an error line from a log file and returns a list of **read-only**
bash commands that gather context about the failure — without making any
modifications to the system.
"""  
from __future__ import annotations  
from .base import Agent
import logging

_logger = logging.getLogger("log_monitor.diagnostic")

_SYSTEM_PROMPT = """\
You are a senior Linux system diagnostic expert.  
When given an error message from a log file, respond with ONLY a JSON array \
of read-only diagnostic commands that will help investigate the root cause.  
RESPONSE FORMAT — raw JSON array only (no markdown, no preamble, no explanation):
[
{"command": "ls -la /path/to/file", "description": "Check file permissions"},
{"command": "id -u",                "description": "Check current user UID"}
]  
STRICT RULES:
1. Return ONLY the JSON array — no text before or after.
2. Commands MUST be read-only: no sudo changes, no writes, no deletes, \
no service restarts, no package installs.
3. Include 3–6 targeted commands directly relevant to the specific error.
4. Use precise, minimal commands (avoid dumping entire large files).
5. If a path appears in the error, include permission/ownership/existence checks for it.
"""

class DiagnosticAgent(Agent):
    """Agent that generates read-only diagnostic commands for log errors."""
    
    def __init__(self):
        super().__init__(
            role="Diagnostic Agent",
            system_prompt=_SYSTEM_PROMPT,
            model=config.OLLAMA_MODEL,
            base_url=config.OLLAMA_HOST,
            timeout=config.LLM_TIMEOUT
        )
    
    def analyze(self, error_line: str) -> list[dict[str, str]]:
        """
        Analyze an error line and return a list of diagnostic commands.
        Returns list of dicts with "command" and "description" keys.
        """
        _logger.debug(f"{self.role} analyzing error: {error_line[:50]}")
        prompt = f"Error line:\n{error_line}\n\nGenerate diagnostic commands."
        raw = self.call(prompt)
        
        try:
            commands = self.parse_json(raw)
        except ValueError as exc:
            _logger.error(f"{self.role} parse error: {exc}")
            raise ValueError(f"DiagnosticAgent: {exc}") from exc
        
        if not isinstance(commands, list):
            _logger.error(f"{self.role} expected list, got {type(commands).__name__}")
            raise ValueError(
                f"DiagnosticAgent expected a JSON array, got {type(commands).__name__}. "
                f"Response: {raw[:200]!r}"
            )
        
        _logger.info(f"{self.role} generated {len(commands)} diagnostic commands")
        return commands
