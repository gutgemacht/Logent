"""
config.py – Central configuration for the Multi-Agent Log Monitor.
Edit this file to point to your Ollama instance and tune behaviour.
"""

# ── Ollama settings ───────────────────────────────────────────────────────────
OLLAMA_BASE_URL: str = "http://192.168.1.100:11434"
OLLAMA_MODEL: str = "llama3.2"          # Change to any model available on your Ollama
OLLAMA_TIMEOUT: int = 120               # Seconds to wait for an LLM response

# ── Log monitoring ────────────────────────────────────────────────────────────
LOGS_CONFIG_FILE: str = "logs.lst"      # One path per line; # = comment

# Regex patterns that flag a line as an error (case-insensitive)
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

# ── Command execution ─────────────────────────────────────────────────────────
COMMAND_TIMEOUT: int = 30               # Max seconds per shell command
MAX_OUTPUT_CHARS: int = 3000            # Truncate long command output before sending to LLM
