"""
agents/solver.py
================
SolverAgent

Receives the original error text plus the combined output of all diagnostic
commands, then returns either:

  A) A list of fix commands, each annotated with a criticality level
     ("safe" / "warning" / "danger") for colour-coded display in the UI,
     and a "sudo" flag for execution.

  B) A special dict asking for additional diagnostics, when the available
     information is insufficient to propose a safe fix.
"""

from __future__ import annotations

from .base import Agent
from core.knowledge import trim_evidence, redact

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
    "expected_result": "The owner can execute the script",
    "verification": "Rerun the approved permission check and test execution as the affected user",
    "undo_command": "",
    "criticality": "warning",
    "sudo": true
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

━━━ RESPONSE FORMAT C (no applicable fix) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
If no safe applicable fix can be proposed, explain why:
{"no_applicable_fix": true, "reason": "Specific missing evidence or constraint"}
Do not return an unexplained empty array.

━━━ RESPONSE FORMAT D (manual or mixed repair) ━━━━━━━━━━━━━━━━━━━━━━━━━━━
{"description":"targeted remedy and its evidence", "commands":[],
 "manual_steps":["specific action the user can take in the application"],
 "verification":"how to retest the original operation"}
Use this when repair requires an application UI, configuration supplied by the user,
or an administrator action. Do not treat absence of shell commands as absence of help.
Historical cases and their ratings are reference only; require the current confirmed
symptom AND confirmed cause. Adapt stored commands to this environment and explain differences.

━━━ CRITICALITY LEVELS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Use EXACTLY one of these strings:
  "safe"    → completely harmless or read-only              (displayed in green)
  "warning" → modifies files / permissions, but reversible  (displayed in yellow)
  "danger"  → affects running services, deletes data,
              restarts processes, or is hard to undo         (displayed in red)

━━━ STRICT RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Return ONLY the JSON — nothing else.
   Solve the current confirmed diagnosis, not a historical error sharing its path.
   Connection timeouts, refusals, DNS failures and port conflicts are network problems.
   Do not propose chmod/chown/chgrp/setfacl for them. Use evidence about the current
   endpoint, listener, route and name resolution; ask for more diagnostics if needed.
   A missing file is not a permissions problem: chmod/chown cannot restore a missing file.
   Before suggesting permission changes, current diagnostics must show the target exists
   and that the permissions are actually incorrect. Check symlink targets too.
   Do not invent file contents or restore from an unverified backup; request more data if needed.
2. Order commands from least risky to most risky.
3. Use targeted, minimal-impact fixes; avoid broad changes.
4. Use Format B ONLY if you genuinely cannot proceed without extra data.
5. If a command requires root/superuser privileges, set "sudo": true. Do NOT put the word 'sudo' inside the "command" string itself.
6. Explain the expected change and verification for each command. Include undo_command only
   when a targeted rollback is supported by the observed original state. Otherwise use an empty string.
"""


class SolverAgent(Agent):
    """
    Proposes fix commands based on the error and diagnostic output.
    Each command is annotated with a ``criticality`` level for UI colouring
    and a ``sudo`` flag for execution.
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
            Each dict has keys ``command``, ``description``, ``criticality``, ``sudo``.
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
        # 1. Reinforce the strict JSON instruction at the end of the user prompt
        prompt = (
            f"Original log error:\n{redact(error_text)}\n\n"
            f"Diagnostic output:\n{trim_evidence(diagnostic_output)}\n\n"
            f"Provide fix commands. REMEMBER: Return strictly raw JSON only. "
            f"No conversational preamble, no explanations, no markdown blocks."
        )
        raw = self.call(prompt)

        # 2. Sanitize the response to extract only the JSON structure
        raw_cleaned = raw.strip()
        
        # Find the first opening bracket ([ or {) and the last closing bracket (] or })
        first_list = raw_cleaned.find('[')
        first_dict = raw_cleaned.find('{')
        
        # Determine the actual start of the JSON
        if first_list != -1 and first_dict != -1:
            start_idx = min(first_list, first_dict)
        else:
            start_idx = max(first_list, first_dict)

        if start_idx != -1:
            # Match the closing bracket type based on the opening bracket
            is_list = raw_cleaned[start_idx] == '['
            end_idx = raw_cleaned.rfind(']') if is_list else raw_cleaned.rfind('}')
            
            if end_idx != -1:
                # Extract just the JSON portion
                raw_cleaned = raw_cleaned[start_idx:end_idx+1]

        try:
            # Parse the cleaned string instead of the raw output
            result = self.parse_json(raw_cleaned)
        except ValueError as exc:
            raise ValueError(f"SolverAgent: {exc}. Cleaned string was: {raw_cleaned[:200]!r}") from exc

        if not isinstance(result, (list, dict)):
            raise ValueError(
                f"SolverAgent expected JSON array or object, got "
                f"{type(result).__name__}. Response: {raw_cleaned[:200]!r}"
            )

        # Normalise list of fix commands
        if isinstance(result, list):
            return self._normalise_fix_commands(result)

        if result.get('manual_steps'):
            steps = result['manual_steps']
            if not isinstance(steps, list) or any(not isinstance(step, str) or not step.strip() for step in steps):
                raise ValueError('Manual steps must be a non-empty list of instructions')
            return {'manual_steps': steps, 'description': str(result.get('description', '')),
                    'verification': str(result.get('verification', '')),
                    'commands': self._normalise_fix_commands(result.get('commands', []))}

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

        if result.get("no_applicable_fix") is True:
            reason = result.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("No-applicable-fix response must explain why")
            return {"no_applicable_fix": True, "reason": reason}

        # Accept the common object wrapper without silently discarding its fixes.
        if "commands" in result:
            if not isinstance(result["commands"], list):
                raise ValueError("Solver commands must be an array")
            return self._normalise_fix_commands(result["commands"])
        if not result.get("command"):
            raise ValueError("Unrecognized solver response: expected fixes, more diagnostics, or an explained no-fix result")

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
            if not isinstance(item, dict) or not isinstance(item.get("command"), str) or not item["command"].strip():
                raise ValueError("Every proposed fix must contain a non-empty command string")
            
            # Parse criticality
            crit = str(item.get("criticality", "safe")).lower()
            if crit not in valid_criticalities:
                crit = "warning"
                
            # Parse sudo flag safely
            needs_sudo = item.get("sudo", False)
            if isinstance(needs_sudo, str):
                needs_sudo = needs_sudo.lower() in ("true", "1", "yes", "y")
            else:
                needs_sudo = bool(needs_sudo)

            result.append(
                {
                    "command": str(item["command"]),
                    "description": str(item.get("description", "")),
                    "expected_result": str(item.get("expected_result", "")),
                    "verification": str(item.get("verification", "")),
                    "undo_command": str(item.get("undo_command", "")),
                    "criticality": crit,
                    "sudo": needs_sudo,
                }
            )
        return result
