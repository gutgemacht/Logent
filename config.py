"""
config.py – Central configuration for the Multi-Agent Log Monitor.
Edit this file to point to your Ollama instance and tune behaviour.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ── Ollama settings ───────────────────────────────────────────────────────────
OLLAMA_BASE_URL: str = "http://localhost:11434"
OLLAMA_MODEL: str = "qwen3:4b-instruct"
OLLAMA_TIMEOUT: int = 600               # Seconds to wait for an LLM response

# ── Log monitoring ────────────────────────────────────────────────────────────
LOGS_CONFIG_FILE: str = str(BASE_DIR / "logs.lst")      # One path per line; # = comment

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
MAX_OUTPUT_CHARS: int = 1500            # Max selected evidence characters per diagnostic command

MAX_DIAGNOSTIC_TOTAL_CHARS: int = 6000  # Entire diagnostic batch, including headers

# Local error/solution history (MiniLM embeddings run on CPU).
HISTORY_ENABLED: bool = True
CHROMA_PATH: str = str(BASE_DIR / '.chroma_db')

REPORTS_PATH: str = str(BASE_DIR / 'reports')
ARROW_MENUS: bool = True
