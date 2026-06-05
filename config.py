"""
config.py – Central configuration for the Multi-Agent Log Monitor.
Edit this file to point to your Ollama instance and tune behaviour.
"""  
from __future__ import annotations  # ← CORRECTED FROM **future**
import logging  # ← Add logging import

# ── LLM Configuration ────────────────────────────────────────────────────────
OLLAMA_HOST: str = "http://127.0.0.1:11434"
OLLAMA_MODEL: str = "llama3.2"
LLM_TIMEOUT: int = 60

# ── Log monitoring ──────────────────────────────────────────────────────────
LOGS_CONFIG_FILE: str = "logs.lst"      # One path per line; # = comment
POLL_INTERVAL: float = 1.0              # Seconds between file checks

ERROR_PATTERNS: list[str] = [
    r"exception",
    r"\berror\b",
    r"denied",
    r"critical",
    r"fatal",
    r"\bfailed\b",
    r"traceback",
    r"permission denied",
]

COMMAND_TIMEOUT: int = 30               # Max seconds per shell command
MAX_OUTPUT_CHARS: int = 3000            # Truncate long command output

LOG_FILE: str = "log_monitor.log"       # File for debug logging
LOG_LEVEL: int = logging.DEBUG          # Verbosity: DEBUG, INFO, WARNING, ERROR
