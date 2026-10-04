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
.venv/bin/python -m pip install -r requirements.txt

# 2. Configure Ollama endpoint and model
vim config.py          # set OLLAMA_BASE_URL and OLLAMA_MODEL

# 3. List the log files/directories to watch
vim logs.lst           # one absolute path per line

# 4. Run
./run.sh
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
- Uses Rich, watchdog and local ChromaDB; Ollama provides LLM responses.
- Ollama must be running and reachable at `OLLAMA_BASE_URL`
- New errors while a pipeline is in progress queue up and are processed after

## Diagnostic verification

Console messages, panel displays, and prompts begin with a timestamp.
The diagnostic agent first proposes a suspected cause and read-only OS commands.
After you approve execution, command results (including success/failure) are sent
back to the diagnostic agent. It returns a confirmed, disproved, or inconclusive
verdict with an explanation. Only confirmed diagnoses proceed to the solver.
For disproved or inconclusive diagnoses, choose **Research further** (1) or
**Ignore** (2). Further research retains previous hypotheses and command results;
each new command set still requires your approval.

Run locally with `./run.sh`. Choices use 1 or 2 followed by Enter.

Diagnostic commands are restricted to read-only inspection tools. Modifying
commands such as `chmod` or `chown` are rejected before approval or execution.
A rejected model plan is retried once with corrective instructions. Diagnostics
use one command at a time without shell wrappers, pipes, or redirects. Repairs
are proposed separately by the solver after the diagnosis is confirmed.

## Local solution history

The configured LLM is `qwen3:4b-instruct`. Install it with:

```bash
ollama pull qwen3:4b-instruct
```

ChromaDB history is enabled by default (`HISTORY_ENABLED` in `config.py`). Data
persists in `.chroma_db/`; the small MiniLM embedding model runs on CPU and is
cached in `.chroma_models/`. The embedding model downloads on the first lookup;
after that, history search works offline. These directories are excluded from Git.

Each detected error is counted, including errors you ignore. Timestamps and PIDs
are removed for exact matching; paths and error codes are preserved. Semantically
related errors appear separately and their statistics are never merged.

Before diagnostics, the history panel shows occurrences and user-confirmed solved
occurrences. Saved solutions list helped/failed/unsure counts and success rates
(yes / (yes + no)); unsure outcomes do not affect the percentage. Solutions are
ranked by confirmed successes, then success rate. Untested entries are labelled.
You can reuse a saved solution, run diagnostics, ignore, or paste your own fix.
Reusing commands still requires approval in the solution panel.

After applying a fix, answer Yes / No / Unsure to whether it solved the problem.
A command's successful exit does not count as a confirmed resolution. Then choose
to save the solution, skip saving it, or paste a different solution. Occurrence
and outcome statistics are retained even when you skip saving a new solution;
reusing an existing solution updates its rating. Pasted solutions are saved but
never executed automatically. Enter notes and optional commands separately,
finishing each multiline input with `.done`. An entry marked Unsure has no
confirmed success until you report one.

If ChromaDB is unavailable, the application reports it and continues diagnostics
without history. Disable history with `HISTORY_ENABLED = False` if desired.

Run tests:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```
