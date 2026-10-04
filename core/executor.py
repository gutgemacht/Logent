"""
core/executor.py
================
Thin wrappers around ``subprocess`` for executing shell commands.

Fix commands support shell syntax. Diagnostic commands are checked against
a read-only executable allowlist and run as argument lists without a shell.
Output is captured; stderr is appended on non-zero exit.
"""

from __future__ import annotations

import subprocess

from core.diagnostic_commands import diagnostic_argv, validate_diagnostic_commands


def run_command(command: str | list[str], timeout: int = 30, *, shell: bool = True) -> tuple[bool, str]:
    """
    Execute a shell command and return ``(success, output)``.

    Parameters
    ----------
    command : str
        Shell command string to execute.
    timeout : int
        Maximum number of seconds to wait before aborting.

    Returns
    -------
    tuple[bool, str]
        ``success`` is True when the exit code is 0.
        ``output`` contains stdout; stderr is appended on failure.
    """
    try:
        result = subprocess.run(
            command,
            shell=shell,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode == 0:
            return True, stdout

        # Non-zero exit: merge both streams for the UI
        combined = stdout
        if stderr:
            combined = f"{stdout}\n[stderr] {stderr}".strip()
        return False, combined

    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout}s."
    except Exception as exc:  # noqa: BLE001
        return False, f"Execution error: {exc}"


def run_commands_collect(
    commands: list[dict[str, str]],
    timeout: int = 30,
    max_output_chars: int = 3000,
) -> str:
    """
    Run a list of command dicts sequentially and return combined output.

    Parameters
    ----------
    commands : list[dict]
        Each dict must have a ``"command"`` key.
    timeout : int
        Per-command timeout in seconds.
    max_output_chars : int
        Truncate individual command output to this many characters so
        the combined result stays within LLM context limits.

    Returns
    -------
    str
        Multi-command output block formatted as::

            $ cmd1
            output1

            $ cmd2
            output2
    """
    validate_diagnostic_commands(commands)
    parts: list[str] = []
    for item in commands:
        cmd = item.get("command", "")
        if not cmd:
            continue
        success, output = run_command(diagnostic_argv(cmd), timeout=timeout, shell=False)
        if len(output) > max_output_chars:
            output = output[:max_output_chars] + "\n[… output truncated …]"
        status = "success" if success else "failed"
        parts.append(f"$ {cmd}\nStatus: {status}\n{output or '(no output)'}")
    return "\n\n".join(parts)
