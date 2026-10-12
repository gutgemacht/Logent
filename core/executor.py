"""
core/executor.py
================
Thin wrappers around ``subprocess`` for executing shell commands.

Fix commands support shell syntax. Diagnostic commands are checked against
a read-only executable allowlist and run as argument lists without a shell.
Output is captured; stderr is retained for both successful and failed commands.
"""

from __future__ import annotations

import subprocess
import os

from core.diagnostic_evidence import relevant_output
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
        ``output`` contains stdout and any stderr.
    """
    try:
        result = subprocess.run(
            command,
            shell=shell,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=None if shell else dict(os.environ, LC_ALL='C'),
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        # Retain warnings on successful commands as diagnostic evidence too.
        combined = stdout
        if stderr:
            combined = f"{stdout}\n[stderr] {stderr}".strip()
        return result.returncode == 0, combined

    except subprocess.TimeoutExpired as exc:
        def partial(value):
            return value.decode(errors='replace') if isinstance(value, bytes) else value or ''
        output = partial(exc.stdout).strip()
        stderr = partial(exc.stderr).strip()
        if stderr:
            output = f'{output}\n[stderr] {stderr}'.strip()
        return False, (output + f"\nCommand timed out after {timeout}s.").strip()
    except Exception as exc:  # noqa: BLE001
        return False, f"Execution error: {exc}"


def run_commands_collect(
    commands: list[dict[str, str]],
    timeout: int = 30,
    max_output_chars: int = 1500,
    *,
    max_total_chars: int = 6000,
    context: str = "",
    capture=None,
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
        Maximum evidence characters per command. Relevant lines are selected
        instead of keeping only the beginning. max_total_chars bounds the
        entire batch including headers; context guides relevance selection.

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
    if max_output_chars < 32 or max_total_chars < 128:
        raise ValueError("Diagnostic evidence limits must be at least 32 per command and 128 total")
    commands = [item for item in commands if item.get('command')]
    if not commands:
        return ''
    # Reserve headers and separators before allocating evidence fairly.
    headers = [f"$ {item['command']}\nStatus: " for item in commands]
    available = max_total_chars - sum(len(header) + len('success') + 1 for header in headers) - 2 * (len(commands) - 1)
    if available < 32 * len(commands):
        return 'Diagnostics not executed: command headers exceed the configured evidence budget. Request fewer or shorter commands.'
    allowance = min(max_output_chars, available // len(commands))
    parts = []
    for item, header in zip(commands, headers):
        success, output = run_command(diagnostic_argv(item['command']), timeout=timeout, shell=False)
        selected = relevant_output(output or '(no output)', allowance,
            ' '.join([context, item['command'], item.get('description', ''), item.get('expected_result', '')]))
        status = 'success' if success else 'failed'
        parts.append(f"{header}{status}\n{selected}")
        if capture:
            capture({'command': item['command'], 'success': success, 'full_output': output,
                     'selected_output': selected, 'shortened': len(output) > allowance,
                     'original_characters': len(output), 'selected_characters': len(selected)})
    return "\n\n".join(parts)
