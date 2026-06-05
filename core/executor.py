"""
core/executor.py
================
Thin wrappers around ``subprocess`` for executing shell commands.  
All commands run with ``shell=True`` so the full bash feature-set is
available (pipelines, redirects, etc.).  Output is captured and returned
as text; stderr is appended on non-zero exit.
"""  
from __future__ import annotations  
import subprocess
import logging

_logger = logging.getLogger("log_monitor.executor")

def run_command(command: str, timeout: int = 30) -> tuple[bool, str]:
    """
    Execute a shell command and return ``(success, output)``.  
    Parameters
    -------
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
    _logger.debug(f"Executing command: {command[:50]}...")
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()  
        
        if result.returncode == 0:
            _logger.debug(f"Command succeeded: {command[:50]}")
            return True, stdout
        else:
            # Non-zero exit: merge both streams for the UI
            combined = stdout
            if stderr:
                combined = f"{stdout}\n[stderr] {stderr}".strip()
            _logger.warning(f"Command failed: {command[:50]}, exit={result.returncode}")
            return False, combined  
    except subprocess.TimeoutExpired:
        _logger.error(f"Command timed out after {timeout}s: {command[:50]}")
        return False, f"Command timed out after {timeout}s."
    except Exception as exc:  # noqa: BLE001
        _logger.error(f"Execution error: {exc}")
        return False, f"Execution error: {exc}"

def run_commands_collect(
    commands: list[dict[str, str]],
    timeout: int = 30,
    max_output_chars: int = 3000,
) -> str:
    """
    Run a list of command dicts sequentially and return combined output.  
    Parameters
    -------
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
    _logger.debug(f"Running {len(commands)} commands sequentially")
    parts: list[str] = []
    for item in commands:
        cmd = item.get("command", "")
        if not cmd:
            continue
        success, output = run_command(cmd, timeout=timeout)
        if len(output) > max_output_chars:
            output = output[:max_output_chars] + "\n[… output truncated …]"
        parts.append(f"$ {cmd}\n{output}" if output else f"$ {cmd}\n(no output)")
    
    result = "\n\n".join(parts)
    _logger.debug(f"Combined output: {len(result)} characters")
    return result
