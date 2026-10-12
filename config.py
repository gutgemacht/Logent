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
WATCHDOG_RULES_FILE: str = str(BASE_DIR / 'watchdog_rules.json')
RUNTIME_PATH: str = str(BASE_DIR / '.logent_runtime')

# Case-insensitive problem signatures. Extend these for application-specific logs.
# Avoid matching every occurrence of "kernel", "memory" or "process".
ERROR_PATTERNS: list[str] = [
    # Application failures, exceptions and severe log levels.
    r"exception", r"\berrors?\b",
    r"(?-i:\b[A-Z][\w.]*(?:Error|Exception)\b)", r"denied", r"\bcritical\b", r"\bfatal\b",
    r"\bfailed\b", r"traceback", r"\b(?:emerg(?:ency)?|alert|panic)\b",
    r"\b(?:assertion.*failed|assert(?:ion)? failure|abort(?:ed|ing)|core dumped)\b",

    # Memory exhaustion / Linux OOM killer (including messages without ERROR).
    r"\bout[- ]of[- ]memory\b",
    r"\boom[-_ ](?:kill(?:er)?|reaper)\b",
    r"\bkilled process\s+\d+\b",
    r"\b(?:cannot allocate memory|page allocation failure|allocation failed|ENOMEM)\b",
    r"\bmemory cgroup\b.*\b(?:oom|limit exceeded)\b",

    # Kernel crashes, lockups and hangs.
    r"\b(?:kernel panic|Oops|BUG):?\b",
    r"\b(?:segfault|segmentation fault|general protection fault|bus error|double fault)\b",
    r"\b(?:unable to handle kernel|kernel NULL pointer dereference|stack overflow|stack smashing)\b",
    r"\b(?:soft|hard)[ -]?lockup\b",
    r"\btask\b.*\bblocked for more than\s+\d+\s+seconds\b",
    r"\brcu[\w]*[ :].*\b(?:stall|stalls|stalled)\b",
    r"\b(?:watchdog|NMI watchdog)\b.*\b(?:lockup|stuck|timeout|timed out)\b",
    r"\bkernel(?:\[\d+\])?:.*\b(?:warn(?:ing)?|BUG|Oops)\b",
    r"\b(?:kernel(?:\[\d+\])?:|WARNING:).*\b(?:Call Trace|CPU:|at [\w./-]+:\d+)\b",

    # Hardware faults, storage/filesystem corruption and thermal problems.
    r"\b(?:machine check|MCE|hardware error|uncorrectable|uncorrected)\b",
    r"\bEDAC\b.*\b(?:CE|UE|fatal)\b",
    r"\b(?:thermal trip|overheat(?:ing|ed)?|temperature above threshold)\b",
    r"\b(?:I/O error|medium error|critical medium|read-only file system|filesystem corruption|corrupt(?:ed|ion)?)\b",
    r"\b(?:EXT[234]-fs|XFS|BTRFS)\b.*\b(?:warning|shutdown|abort|inconsistency)\b",
    r"\b(?:nvme|ata\d*|scsi|blk_update_request|end_request)\b.*\b(?:timeout|timed out|resetting|offline|UNC|I/O)\b",
    r"\b(?:no space left on device|disk full|ENOSPC|quota exceeded)\b",

    # Network problems and resource exhaustion.
    r"\b(?:connection refused|connection reset|connection timed out|timed out|timeout)\b",
    r"\b(?:network is unreachable|no route to host|temporary failure in name resolution|could not resolve|failed to resolve)\b",
    r"\b(?:address already in use|too many open files|resource temporarily unavailable|cannot fork)\b",
    r"\b(?:NETDEV WATCHDOG|transmit queue.*timed out)\b",

    # Service/process failures often logged without an ERROR prefix.
    r"\b(?:main process exited|failed with result|start request repeated too quickly|dependency failed)\b",
    r"\b(?:code=(?:dumped|killed)|status=\d+/(?:FAILURE|SEGV|ABRT|KILL))\b",
    r"\bunit\b.*\bnot found\b",
    r"\b(?:file|directory|command)\b.*\b(?:not found|does not exist|not present)\b",
    r"\bno such file or directory\b",
]
# Syslog PRI includes facility and severity; severity 0–3 means emerg–err.
ERROR_PATTERNS.append(r"^<(?:" + "|".join(str(facility * 8 + severity)
                     for facility in range(24) for severity in range(4)) + r")>")

# ── Command execution ─────────────────────────────────────────────────────────
COMMAND_TIMEOUT: int = 30               # Max seconds per shell command
MAX_OUTPUT_CHARS: int = 1500            # Max selected evidence characters per diagnostic command

MAX_DIAGNOSTIC_TOTAL_CHARS: int = 6000  # Entire diagnostic batch, including headers

# Local error/solution history (MiniLM embeddings run on CPU).
HISTORY_ENABLED: bool = True
CHROMA_PATH: str = str(BASE_DIR / '.chroma_db')

REPORTS_PATH: str = str(BASE_DIR / 'reports')
ARROW_MENUS: bool = True
