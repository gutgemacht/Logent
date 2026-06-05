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

# ── System prompt ─────────────────────────────────────────────────────────────
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
    """
    Analyses an error message and returns read-only diagnostic commands
    to gather environment context.
    """

    def __init__(self, model: str, base_url: str, timeout: int = 120) -> None:
        super().__init__(
            role="Diagnostic Agent",
            system_prompt=_SYSTEM_PROMPT,
            model=model,
            base_url=base_url,
            timeout=timeout,
        )

    def analyze(self, error_text: str) -> list[dict[str, str]]:
        """
        Generate diagnostic commands for the given error line.

        Parameters
        ----------
        error_text : str
            The raw error line captured from the log file.

        Returns
        -------
        list[dict[str, str]]
            List of ``{"command": "...", "description": "..."}`` dicts.

        Raises
        ------
        RuntimeError
            If the Ollama API call fails.
        ValueError
            If the model response cannot be parsed as a JSON array.
        """
        prompt = f"Analyze this log error and return diagnostic commands:\n\nERROR:\n{error_text}"
        raw = self.call(prompt)

        try:
            commands = self.parse_json(raw)
        except ValueError as exc:
            raise ValueError(f"DiagnosticAgent: {exc}") from exc

        if not isinstance(commands, list):
            raise ValueError(
                f"DiagnosticAgent expected a JSON array, got {type(commands).__name__}. "
                f"Response: {raw[:200]!r}"
            )

        # Normalise: ensure every entry has at least 'command'
        result: list[dict[str, str]] = []
        for item in commands:
            if isinstance(item, dict) and "command" in item:
                result.append(
                    {
                        "command": str(item["command"]),
                        "description": str(item.get("description", "")),
                    }
                )
        return result
