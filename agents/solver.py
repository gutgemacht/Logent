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

# ── System prompt ─────────────────────────────────────────────────────────────
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
  "safe"    → completely harmless or read-only              (displayed in green)
  "warning" → modifies files / permissions, but reversible  (displayed in yellow)
  "danger"  → affects running services, deletes data,
              restarts processes, or is hard to undo         (displayed in red)

━━━ STRICT RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Return ONLY the JSON — nothing else.
2. Order commands from least risky to most risky.
3. Use targeted, minimal-impact fixes; avoid broad changes.
4. Use Format B ONLY if you genuinely cannot proceed without extra data.
"""


class SolverAgent(Agent):
    """
    Proposes fix commands based on the error and diagnostic output.
    Each command is annotated with a ``criticality`` level for UI colouring.
    """

    def __init__(self, model: str, base_url: str, timeout: int = 120) -> None:
        super().__init__(
            role="Solver Agent",
            system_prompt=_SYSTEM_PROMPT,
            model=model,
            base_url=base_url,
            timeout=timeout,
        )

    def solve(
        self, error_text: str, diagnostic_output: str
    ) -> list[dict] | dict:
        """
        Generate fix commands from error + diagnostic context.

        Parameters
        ----------
        error_text : str
            The original error line from the log file.
        diagnostic_output : str
            Combined stdout of all diagnostic commands.

        Returns
        -------
        list[dict]
            Fix commands when data is sufficient.
            Each dict has keys ``command``, ``description``, ``criticality``.
        dict
            ``{"need_more_diagnostics": True, "reason": "...", "commands": [...]}``
            when the model needs more context.

        Raises
        ------
        RuntimeError
            If the Ollama API call fails.
        ValueError
            If the response cannot be parsed.
        """
        prompt = (
            f"Original log error:\n{error_text}\n\n"
            f"Diagnostic output:\n{diagnostic_output}\n\n"
            f"Provide fix commands."
        )
        raw = self.call(prompt)

        try:
            result = self.parse_json(raw)
        except ValueError as exc:
            raise ValueError(f"SolverAgent: {exc}") from exc

        if not isinstance(result, (list, dict)):
            raise ValueError(
                f"SolverAgent expected JSON array or object, got "
                f"{type(result).__name__}. Response: {raw[:200]!r}"
            )

        # Normalise list of fix commands
        if isinstance(result, list):
            return self._normalise_fix_commands(result)

        # Normalise Format B (need_more_diagnostics)
        if result.get("need_more_diagnostics"):
            return {
                "need_more_diagnostics": True,
                "reason": str(result.get("reason", "No reason provided")),
                "commands": [
                    {
                        "command": str(c.get("command", "")),
                        "description": str(c.get("description", "")),
                    }
                    for c in result.get("commands", [])
                    if isinstance(c, dict) and c.get("command")
                ],
            }

        # Fallback: treat dict as a single-item list
        return self._normalise_fix_commands([result])

    # ──────────────────────────────────────────────────────────────────
    # Private helpers
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _normalise_fix_commands(items: list) -> list[dict]:
        """Ensure every fix command dict has the required keys."""
        valid_criticalities = {"safe", "warning", "danger"}
        result = []
        for item in items:
            if not isinstance(item, dict) or not item.get("command"):
                continue
            crit = str(item.get("criticality", "safe")).lower()
            if crit not in valid_criticalities:
                crit = "warning"
            result.append(
                {
                    "command": str(item["command"]),
                    "description": str(item.get("description", "")),
                    "criticality": crit,
                }
            )
        return result
