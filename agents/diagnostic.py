"""
agents/diagnostic.py
====================
DiagnosticAgent

Receives an error line from a log file and returns a list of **read-only**
bash commands that gather context about the failure — without making any
modifications to the system.
"""

from __future__ import annotations

import sys
from typing import List, Dict, Any
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
        print("\n[DiagnosticAgent] Initialization complete")

    def analyze(self, error_text: str) -> List[Dict[str, str]]:
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
        print("\n[DiagnosticAgent] Starting error analysis")

        # 1. Update prompt to enforce raw JSON and double-escaping for regex
        prompt = (
            f"Analyze this log error and return diagnostic commands:\n\n"
            f"ERROR:\n{error_text}\n\n"
            f"REMEMBER: Return strictly raw JSON array only. No preamble. "
            f"If your commands include backslashes (e.g., for grep regex), "
            f"you MUST double-escape them in the JSON string (e.g., write \\\\. instead of \\.)."
        )
        print(f"[DiagnosticAgent] Sending request to model with prompt: {prompt}")

        try:
            print("[DiagnosticAgent] Waiting for API response...")
            raw = self.call(prompt)
            print("[DiagnosticAgent] Received API response")
        except Exception as e:
            print(f"[DiagnosticAgent] Error during API call: {e}", file=sys.stderr)
            raise

        print("\n[DiagnosticAgent] Parsing JSON response")

        # 2. Sanitize the response to extract only the JSON array
        raw_cleaned = raw.strip()
        start_idx = raw_cleaned.find('[')
        end_idx = raw_cleaned.rfind(']')

        if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
            raw_cleaned = raw_cleaned[start_idx:end_idx+1]

        try:
            commands = self.parse_json(raw_cleaned)
        except ValueError as exc:
            print(f"\n[DiagnosticAgent] Error parsing JSON: {exc}\nCleaned string was: {raw_cleaned[:200]!r}", file=sys.stderr)
            raise

        if not isinstance(commands, list):
            print(
                f"\n[DiagnosticAgent] Invalid response type - expected list got {type(commands).__name__}",
                file=sys.stderr
            )
            raise ValueError(
                f"DiagnosticAgent expected a JSON array, got {type(commands).__name__}. "
                f"Response: {raw_cleaned[:200]!r}"
            )

        # Normalise: ensure every entry has at least 'command'
        print("\n[DiagnosticAgent] Normalizing command list")
        result: List[Dict[str, str]] = []
        for item in commands:
            if isinstance(item, dict) and "command" in item:
                result.append(
                    {
                        "command": str(item["command"]),
                        "description": str(item.get("description", "")),
                    }
                )

        print(f"\n[DiagnosticAgent] Generated {len(result)} diagnostic commands")
        return result
