второе изменение
первое изменение

# Multi-Agent Log Monitor

A lightweight Python multi-agent system for real-time log monitoring, automated
diagnostics, and interactive error remediation — powered by a local Ollama LLM.

## Architecture

```
log_monitor/
├── main.py                 ← Entry point
├── config.py               ← All tuneable settings
├── logs.lst                ← Paths to monitor (one per line)
├── requirements.txt
├── agents/
│   ├── base.py             ← Base Agent (HTTP call + JSON parsing)
│   ├── diagnostic.py       ← Generates read-only diagnostic commands
│   └── solver.py           ← Generates colour-coded fix commands
├── core/
│   ├── watcher.py          ← watchdog-based file monitor
│   ├── executor.py         ← subprocess command runner
│   └── orchestrator.py     ← Pipeline coordinator
└── ui/
    └── display.py          ← Rich terminal UI (3 interactive panels)
```

## Pipeline

```
[watchdog thread]          [main thread]
     │
     │  new error line
     └──────────────────►  Block 1: Show error
                                │   [1] Confirm  [2] Ignore
                                ▼
                           DiagnosticAgent → LLM
                                │
                           Block 2: Show diagnostic commands
                                │   [1] Allow  [2] Deny
                                │
                           subprocess (read-only commands)
                                │
                           SolverAgent → LLM
                                │
                           Block 3: Show fix commands (colour-coded)
                                │   [1] Apply  [2] Skip
                                │
                           subprocess (fix commands, one-by-one)
```

## Criticality colours

| Level   | Colour | Meaning                                        |
|---------|--------|------------------------------------------------|
| safe    | Green  | Read-only or harmless                          |
| warning | Yellow | Modifies files/permissions, but reversible     |
| danger  | Red    | Affects services, deletes data, hard to undo   |

## Setup

```bash
# 1. Install Python dependencies
pip install rich watchdog

# 2. Configure Ollama endpoint and model
vim config.py          # set OLLAMA_BASE_URL and OLLAMA_MODEL

# 3. List the log files/directories to watch
vim logs.lst           # one absolute path per line

# 4. Run
python main.py
```

## Use-case example

1. `/var/log/auth.log` receives a line:
   `"Cannot execute deploy.sh, permission denied"`
2. **Block 1** displays it. User presses `1` (Confirm).
3. **Diagnostic Agent** returns `ls -la deploy.sh`, `id -u`, `stat deploy.sh`.
4. **Block 2** asks permission. User presses `1` (Allow). Commands run.
5. **Solver Agent** sees no execute bit → proposes `chmod +x deploy.sh` (⚠ warning).
6. **Block 3** shows it in yellow. User presses `1` (Apply). Command runs. ✓ Done.

## Notes

- Requires Python 3.10+
- No LangChain, AutoGen, CrewAI or any AI framework — pure stdlib + thin UI/watch libs
- Ollama must be running and reachable at `OLLAMA_BASE_URL`
- New errors while a pipeline is in progress queue up and are processed after
