# Multi-Agent Log Monitor

A lightweight Python multi-agent system for real-time log monitoring, automated
diagnostics, and interactive error remediation — powered by a local Ollama LLM.

## Architecture

```
Logent/
├── main.py                 ← Entry point
├── config.py               ← All tuneable settings
├── logs.lst                ← Paths to monitor (one per line)
├── requirements.txt
├── streamlit_access.py     ← Browser history, configuration and health dashboard
├── streamlit_access.sh     ← Dashboard launcher using the project environment
├── agents/
│   ├── base.py             ← Base Agent (HTTP call + JSON parsing)
│   ├── diagnostic.py       ← Generates read-only diagnostic commands
│   └── solver.py           ← Generates colour-coded fix commands
├── core/
│   ├── watcher.py          ← watchdog-based file monitor
│   ├── executor.py         ← subprocess command runner
│   ├── history.py          ← ChromaDB problems, solutions, feedback and cases
│   ├── monitoring.py       ← Monitor heartbeat, queue and trigger telemetry
│   └── orchestrator.py     ← Pipeline coordinator
└── ui/
    ├── investigation.py    ← Permanent current-error and progress area
    └── display.py          ← Rich terminal dialogs and evidence panels
```

## Pipeline

```
Detected: show the error, source, trigger and case number
    ↓ Review or ignore
History checked: exact and compatible similar cases, statistics and reuse conditions
    ↓ Diagnose, review a suitable saved solution, or continue monitoring
Diagnosing: approve read-only checks → inspect selected evidence → confirm the cause
    ↓ Research further if the hypothesis is disproved or inconclusive
Fix proposed: review effects, privileges, risk and available undo instructions
    ↓ Apply approved commands, or skip
Verification: rerun approved checks to assess whether the original problem persists
    ↓ Report Fixed / Still failing / Cannot verify / Skip feedback
Outcome saved: retain the case timeline; saving a new solution is a separate choice
    ↓ Case summary → continue monitoring queued and new events
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
2. The permanent error area displays it with a case number. The user reviews it.
3. **Diagnostic Agent** returns `ls -la deploy.sh`, `id -u`, `stat deploy.sh`.
4. The user approves the read-only checks. Their selected output appears on screen
   and returns to the diagnostic agent to confirm or disprove the hypothesis.
5. If the evidence confirms the missing execute bit, **Solver Agent** proposes
   `chmod +x deploy.sh` with its expected effect and verification checks.
6. The user approves the fix, reruns the checks and reports the outcome.
7. The user can save the solution independently of reporting whether it worked.
   The case summary appears and monitoring continues.

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

Run locally with `./run.sh`. Use arrow-key menus when supported, or numbered choices followed by Enter.

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

The initial history lookup supplies compatible read-only diagnostic templates,
not repair commands. After diagnostics confirm the symptom and cause, the history
panel shows occurrences and user-confirmed solved occurrences.
Saved solutions list helped/failed/unsure counts and success rates
(yes / (yes + no)); unsure outcomes do not affect the percentage. Solutions are
ranked by confirmed successes, then success rate. Untested entries are labelled.
You can select a saved solution as repair reference, generate a solution from the confirmed diagnosis, ignore, or
paste your own fix. Related-error solutions are historical context only and
cannot be selected for direct execution.
Every saved repair now requires fresh symptom and cause diagnostics, followed by command approval.

After applying a fix, choose Fixed / Still failing / Cannot verify / Skip feedback.
A command's successful exit does not count as a confirmed resolution. Then choose
to save the solution, skip saving it, or paste a different solution. Occurrence
and outcome statistics are retained even when you skip saving a new solution;
reusing an existing solution updates its rating. Pasted solutions are saved but
never executed automatically. Enter notes and optional commands separately,
finishing each multiline input with `.done`. An entry marked Cannot verify has no
confirmed success until you report one.

If ChromaDB is unavailable, the application reports it and continues diagnostics
without history. Disable history with `HISTORY_ENABLED = False` if desired.

Run tests:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

New errors require a review/ignore choice. In numbered prompts, blank Enter does
not ignore them; arrow menus start with Review highlighted. Similarity history never suppresses new events. If terminal
input closes, the monitor stops without recording an ignore decision.

Choice labels are bold. After each processed error, the monitor reports that it
is continuing to monitor for new errors. Missing-file and permissions errors are
kept separate even when they refer to the same path. Diagnostics include a fresh
`stat -L` check for reported paths. Permissions changes are rejected as solutions
for missing-file reports; historical success does not confirm the current cause.

## Evidence, reuse conditions and verification

Each diagnosis separates the reported symptom, suspected cause, current file facts,
and evidence-based verdict. The agent can cite executed commands and their observed
results, and list remaining uncertainties. Command tables show purpose and expected
results. The local context includes OS, user, machine identity, explicitly named
`.service` units, and file existence/permissions/ownership (following symlinks).

Saved agent fixes include the pre-fix file conditions and read-only verification
commands. Stored conditions explain the environment in which a repair worked. Every selected
repair is sent through fresh diagnosis and adaptation before it can be applied. Older
entries without conditions, manually pasted entries, and obsolete entries remain
available for inspection but require a fresh diagnosis before direct reuse.
Statistics are scoped by machine/OS/user; older unscoped records are preserved and
can be inspected through **Manage history → View details** and in the dashboard.

After a fix, the script offers to rerun the diagnostic checks, then evaluates whether
the original problem still exists. A disproved original problem means the evidence
suggests the fix worked; confirmed means it persists; inconclusive means unknown.
Your reported outcome remains separate from automatic verification. Remaining
fix commands are stopped if one fails.

## Terminal controls and history tools

On supported POSIX terminals use **↑/↓** to highlight an option and **Enter** to
confirm. Typing an option number selects it, including numbers above 9. Basic,
redirected, or dumb terminals retain numbered prompts. Set `ARROW_MENUS = False`
to use numbered prompts everywhere. Input typed during a previous operation is
cleared before a new arrow menu to prevent accidental choices.

A status area shows monitoring/queued errors, the current LLM operation, and elapsed
time. **Ctrl+C during an LLM request** offers Retry or Cancel this error. **Ctrl+C
while monitoring or choosing** stops the monitor. Identical queued errors within a
five-minute window are grouped for one review while every occurrence is retained; different errors are
not merged. A grouped outcome applies to each occurrence in that reviewed group.

The history pane keeps a visible scrollbar while the error header and repair choices
stay fixed. Use **PgUp/PgDn**, **Home/End**, or the **mouse wheel** to scroll it.
Basic or redirected terminals print the complete history without interactive controls.

Choose **Manage history** after diagnosis to view solution details, edit commands
or notes, mark an entry obsolete, or delete an entry. Editing creates a new untested
version so new commands do not inherit the old version's rating. Previous attempt
records are preserved. Solution history shows when a fix last worked.

After reviewing an error, you can export a Markdown and JSON troubleshooting report
containing the error, system context, diagnostic evidence, proposed/executed commands,
verification and user outcome. Reports are saved under `reports/` (`REPORTS_PATH` in
`config.py`) and excluded from Git. They may contain paths and command output from
your system, so review the files before sharing.

### Bounded diagnostic evidence

Diagnostic output is selected locally using the error text, command purpose,
and expected result. Relevant lines and failure messages take priority over
unrelated lines. Selected lines keep their original order; `[… omitted …]`
marks removed text. This is a heuristic and may omit useful context. Short
output is retained unchanged, including stderr warnings on successful commands.

`MAX_OUTPUT_CHARS` (default 1,500) limits evidence per command.
`MAX_DIAGNOSTIC_TOTAL_CHARS` (default 6,000) limits each complete batch,
including command names and statuses. These limits cover initial diagnosis,
additional diagnostics, and checks after a fix. They do not limit the entire
LLM prompt, which also includes system facts and previous conclusions.
The **Diagnostic evidence sent to LLM** panel shows the exact filtered evidence
used in that request, also included in exported reports. If command headers
alone exceed the budget, the batch is not executed and a message asks for fewer
or shorter commands. Increase the configuration limits when more detail is needed.

### When the solver cannot propose a fix

An empty result leaves the error unresolved. The script shows the solver's
reason when available and offers **Retry solver**, **Research further**,
**Save my own solution** (when history is available), or **Continue monitoring**
before offering optional report export. An unexplained empty result is labelled
as such; it is not counted as a successful repair. Further research retains the
previous evidence. Additional diagnostic requests are never executed as fixes.
Malformed solver responses are reported as response errors instead of silently
being converted to an empty fix list.

### Current error stays visible

On supported interactive terminals, the error and its source occupy a fixed
area at the top while history, diagnostics, fix dialogs, verification and report
export scroll below. The reserved area is released when that error's review ends,
including cancellation or closed input, and updates for the next error. Terminal
resizing adjusts the header. Very long errors are visibly clipped to leave room
for dialogs; the full error remains available in exported reports. Redirected,
dumb and very small terminals keep the sequential output.

### Reading ChromaDB statistics

After symptom and cause confirmation, the history display separates the exact error from similar
cases retrieved for the current machine, OS and user. Each case shows occurrence
counts, user-confirmed solves, reported failures, unsure outcomes, occurrences
without a reported outcome, and how many solves have no saved solution available.
The current occurrence is included. Ignoring an error does not count as failure.
An occurrence with any confirmed success is counted as solved; otherwise its latest
reported outcome is used. Solution ratings count individual feedback records.

Similar-case panels show saved commands (including sudo), notes and per-solution
ratings, or explicitly state that no solution was saved. Similarity search retrieves
extra candidates before rejecting incompatible problem types (for example, missing
files versus permission errors, or missing executables versus memory exhaustion).
Unknown errors require specific shared symptom terms and a stricter vector-distance
threshold. Relevant shared terms rank candidates before vector distance; similarity
is not a probability that a fix will work. Older scenarios are reclassified when read.
Similar-case fixes are references for adapting repairs to the current diagnosis.
An empty solver response means no repair was proposed, not that repair is impossible.

### How saved-error matching works

Exact matches use normalized error text (timestamps and volatile PIDs removed;
paths, endpoints, ports and error codes retained), scoped by machine, OS and user.
Similar matches use CPU MiniLM embeddings in ChromaDB: retrieve up to 20 candidates,
keep distances at most 1.0, require the same detected problem type, exclude the
exact error, and show the nearest three. The monitor can also show other environments as reference
knowledge requiring explicit compatibility checks. Unknown problem types only match unknown
ones. Connection timeout/refusal, DNS failure, address-in-use, permissions, missing
files, missing service units and full disks are distinguished before showing matches.
Embedding distance is a relevance filter, not a probability that a fix will work.

Solutions are linked to an error key rather than searched independently. Ratings
sort solutions by successful feedback, then success rate, then fewer failures.
All saved solutions are reference knowledge. Exact and similar fixes require fresh
symptom and cause checks, adaptation, and user approval before application. Incorrect old permission
fixes for network errors are retained, labelled unsuitable and excluded from reuse
and LLM repair hints. New inappropriate fixes are rejected before approval and
the solver gets one corrective attempt, then the unresolved-error menu.

Network diagnostics support read-only ss/netstat, getent lookups and ip show/list
or route get. Socket killing, socket dump-file output and ip modifications remain
blocked. Network-only reports cannot be diagnosed solely through file metadata.

### Kernel and critical problem detection

`ERROR_PATTERNS` in `config.py` now covers severe log levels, exception/error
class names, out-of-memory and OOM-killer events, allocation failures, kernel
panics/oopses, segfaults, CPU lockups, hung tasks/RCU stalls, hardware/ECC faults,
thermal problems, storage/filesystem failures, resource exhaustion, network
failures and service crashes. Syslog PRI severities emerg/alert/crit/err are
recognized even without a severity word in the message. For example:

```text
kernel: Out of memory: kill process 1234 (worker) score 900
kernel: watchdog: BUG: soft lockup - CPU#1 stuck for 23s!
kernel: INFO: task worker:123 blocked for more than 120 seconds.
```

These patterns inspect newly appended lines in files configured in `logs.lst`.
They do not subscribe directly to dmesg/journald or reprocess old log content.
Include the relevant readable system/kernel log files in `logs.lst`. No finite
pattern list covers every application's wording; add custom signatures as needed.
Restart the monitor after editing patterns.

### Browser history manager

Run the rewritten Streamlit page from the Logent directory:

```bash
.venv/bin/python -m pip install -r requirements-dashboard.txt
./streamlit_access.sh
```

Open `http://localhost:8501`. The page uses the absolute `CHROMA_PATH` from
`config.py` and Logent's existing history/statistics code. It shows all saved
problems, including legacy environments and cases without solutions. Use text
search or CPU semantic search, inspect ratings, add problem occurrences and
solutions, edit solutions as new untested versions, mark entries obsolete/active,
record feedback against an existing occurrence, and export a selected case.
Pasted commands are stored as text and are never executed by the dashboard.
Use **Refresh data** to reload changes made by the monitor.
The **Watchdog configuration** tab edits enabled keywords and regex triggers,
previews matches against pasted log lines, and saves them to `watchdog_rules.json`.
Keyword matching uses whole words/phrases and ignores letter case. The Linux
failure patterns remain included in the default rules. Newly started monitors
reload saved rules on the next log update; restart older monitor processes once
after installing this feature. An empty or fully disabled list stops detection.
Log sources are displayed from `logs.lst`; editing sources requires a monitor restart.
The source editor can add or remove files/directories and check their availability
before saving. **Ignore** trigger rules override detection rules for matching lines;
the preview explains both decisions. The monitor also records the matched triggers.

The terminal and dashboard share this investigation pipeline:
`Detected → History checked → Diagnosing → Fix proposed → Verification → Outcome saved`.
Each review gets a short `Case #XXXXXXXX` identifier. Its timeline is stored in the
`logent_investigations` Chroma collection, including hypotheses, command approval
stages, complete captured diagnostic output, the shortened evidence sent to the
LLM, proposed fixes, execution results and verification. Existing historical entries
remain available; missing timelines or automatic verification are explicitly labeled.

Diagnosis distinguishes suspected, confirmed, disproved and inconclusive conclusions.
Confirmed/disproved agent conclusions must cite an actually approved diagnostic
command. Fix previews explain expected effects, required privileges, verification
and an undo command when the original state supports one. Undo commands are shown
for review and are never run automatically. User feedback is **Fixed**, **Still
failing**, **Cannot verify**, or **Skip feedback**, independently of saving a solution.
Automatic verification is tracked separately from user ratings; conflicting outcomes
mark a case as needing review. Skipping feedback does not create a rating.

The **Investigations** tab searches cases and shows their timelines and complete
command output. Selecting a problem row opens a details dialog and selects the same
problem for the editable details below the table. History filters cover unresolved occurrences, missing or
tested solutions, environment, error category and occurrence date. Overview totals
always describe the entire database; filtered tables describe the matching subset.

The **Monitoring health** tab refreshes every three seconds. It reads local
heartbeats in `.logent_runtime/` to show running/stopped/stale monitors, source
readability, the active case, queued events and recent trigger decisions. Start or
restart the terminal monitor after upgrading to enable this telemetry. The web page
does not execute troubleshooting commands or start monitor processes.
Identical events within a five-minute incident window are grouped without dropping
different errors. Repeats arriving during an investigation remain recorded with
unreported outcomes; earlier feedback is not automatically applied to later arrivals.
The launcher always uses Logent's `.venv`, even when a Conda environment is active.
If port 8501 is already occupied, use the local URL printed by Streamlit.

This uses a native Streamlit connection rather than `streamlit-chromadb-connection`
1.0.5, which pins ChromaDB to 0.5.3 and conflicts with Logent's ChromaDB 1.x
requirement. Install dashboard dependencies in Logent's `.venv` instead of
installing the legacy connector into that environment. No database migration or
server-mode conversion is required for this local page.


### Structured troubleshooting knowledge

The diagnostic agent assesses the **current symptom** (`Confirmed`, `Not reproduced`,
`Inconclusive`) independently from the **suspected cause** (`Confirmed`, `Rejected`,
`Inconclusive`). A rejected cause does not close a confirmed symptom. Choose
**Investigate further**, **Watch for recurrence**, or **Ignore this occurrence** when
the evidence does not establish both. Saved solutions always pass through this
fresh diagnostic process. Only confirmed symptoms with confirmed causes reach repair.
Additional evidence requested by the solver is reassessed before another repair proposal.

The `logent_diagnostics` collection stores read-only diagnostic scenarios, hypotheses,
questions, expected evidence, optional objective file criteria, measured conclusions,
source, environment, application/version, and related case IDs. The **Diagnostic
scenarios** dashboard tab can inspect, export, add, validate, edit as a new version,
and deactivate these scenarios. `{target_path}` is resolved as a single quoted
argument and every resolved command is checked against the diagnostic allowlist.
Neither validation nor saving runs any commands. Edited scenarios retain old evidence
on the previous version; new versions start without execution evidence.

File criteria distinguish actual measurements from expected policy. The agent cannot
invent required file modes: an octal requirement must be explicit in the current error
or supplied by a user-authored scenario for the current target/environment. Unknown or
unmet required criteria prevent automatic verification from claiming resolution.
The agent cites captured evidence IDs; Logent attaches the recorded command, status,
and output. Invalid citations yield an inconclusive result with further-research options.
A successful `which` lookup of the reported missing executable establishes that
the absence is not reproduced in the current diagnostic PATH, without another LLM
request. The original application's environment may differ; the log itself never
proves that a symptom is currently present.
An unsuccessful, empty `which` lookup confirms executable unavailability in the
current PATH, not global package absence. For the known Debian-family `mc` and
`screen` packages, diagnostics also propose `dpkg-query --status` and `apt-cache
policy`. Explicit missing-package state and an available repository candidate
allow a targeted installation proposal, still requiring the normal repair approval.
Installed packages with missing executables require PATH/environment investigation.
Manual repair instructions are supported alongside shell commands and require the
user to report completion before verification. Command exit success alone is not resolution.

Statistics distinguish **log events**, **investigation incidents**, and **application
attempts**. One application covering many repeated events receives one rating.
Applications remain recorded when feedback or solution saving is skipped. The
**Existing application attempt** selector updates a rating rather than adding a vote,
and retains an audit trail. New manual attempts use a stable draft across repeated
submissions; use **Start another manually performed attempt** for a separate test.
Automatic verification remains separate from user ratings; contradictory outcomes
mark a case as needing review. Errors detected again after a resolved case link to
that earlier case without automatically lowering its solution rating.

Schema version 2 adds diagnosis, manual instructions, objective criteria, attempt IDs,
and feedback history without deleting earlier records. Legacy grouped case feedback
is counted once per case and solution when computing ratings; legacy records without
case/attempt IDs remain available with their original counts. Existing historical
cases without separate symptom/cause evidence display **Not recorded**.

Potential password/token values and authorization headers are redacted before new
storage and LLM requests. Historical cards and accumulated diagnostic evidence are
bounded for CPU inference; the complete captured case evidence stays available locally.

### Local Linux reference knowledge

Logent imports 25 versioned Linux x64 reference scenarios into the separate
`logent_linux_library` Chroma collection. Categories are `default-linux-kernel`,
`default-linux-filesystem`, `default-linux-permissions`, `default-linux-memory`,
`default-linux-network`, `default-linux-services`, `default-linux-packages`,
`default-linux-resources`, `default-linux-hardware`, and `default-linux-application`.
The importer runs when the history store opens; it preserves existing records,
ratings and disabled references. To import explicitly:

```bash
.venv/bin/python seed_linux_knowledge.py
```

After you confirm an error, Logent looks for an unambiguous reference signature
and fills its read-only command parameters from that error. Exact saved diagnostic
plans from the same environment can also avoid plan regeneration. Similarity alone
never proves that a previous cause or repair applies. Missing targets, conflicting
signatures, unsupported environments and missing tools fall back to the LLM.

Each concrete diagnostic command still needs approval. Narrow local rules can
assess missing paths, explicit required file modes, exhausted filesystem capacity,
missing system units and occupied local TCP addresses. The existing known Debian
package checks distinguish absent executables from absent packages. Complex or
inconclusive evidence goes to the LLM. A historical OOM log, available host RAM,
or a local listener list does not establish the current cause in another container
or remote system.

A confirmed local diagnosis can propose reference manual guidance without an LLM
solver request. Known Debian package installation proposals keep their separate
approval. Verification reruns fresh checks and feedback records actual results;
reference examples never become fake occurrences or successful repairs.

In the dashboard, **Linux knowledge** provides category/search filters, diagnostic
and repair guidance, source documentation, export, enable/disable controls and a
plan preview that executes nothing. **Diagnostic scenarios** remains available
for your own customized checks. Restart the CLI to load changed Python code;
reference activation is read from Chroma for each investigation.

### Semantic search and deleting history

Enable **Search by meaning** to search saved error text and active solution notes,
manual steps and commands. The CPU search index combines MiniLM embeddings with
matching words and compatible symptom types. Common Linux vocabulary in English,
German and Russian is expanded locally; this is domain vocabulary support, not a
multilingual model. Category, environment, date and outcome filters apply before
the result limit. Irrelevant matches are omitted, and the result table explains
why each record matched. **Linux knowledge** offers the same semantic search for
bundled references. Search never establishes a cause or executes a command.

Under the selected problem, **Delete saved problem** previews the number of linked
cases, occurrences, solutions, attempts/ratings and derived diagnostic scenarios.
A confirmation checkbox and **Delete problem permanently** remove these records.
In **Investigations**, **Delete investigation case** removes only that case, its
attempts/ratings and occurrences not shared with retained records. The parent
problem and shared solution knowledge remain. Counts and ratings are recomputed.
Active investigations must be finished or cancelled first.

Deletion preserves bundled Linux references and independent user-authored
scenarios. It removes derived search-index entries and cleans shared scenario and
recurrence links. Existing exported report files remain outside ChromaDB. A future
log occurrence can create new history for a previously deleted problem.

### Automatic saved problem actions and email groups

In **Saved problems and solutions**, edit **Automate** and **Inform**, then click
**Save problem actions**. Automation binds to the displayed active saved solution.
The running terminal monitor matches normalized error text in the same machine,
OS and user environment (timestamps and PIDs may differ). It executes saved
commands without interactive approval, stops on the first command failure, and
records command output and errors under **Traced problem**. Saved file conditions,
when present, must still match. Manual instructions and obsolete solutions cannot
be automated. After editing a solution, disable and re-enable automation to select
the new version. Successful command exit does not count as a verified repair or
user success rating. Repeated events in one grouped incident cause one repair.

Use **Email groups** to save group names and comma-separated recipient addresses,
then select a group in **Inform**. Notifications are sent once per grouped incident;
automated incidents include the repair trace. SMTP delivery successes and failures
are recorded in the timeline. Deleting a group prevents delivery to that group.

Configure SMTP in **Watchdog configuration → Email configuration** and click
**Save SMTP settings**. Saved settings are read on the next notification. The
password is masked in the UI and stored in `.logent_runtime/email/smtp-settings.json`
with file permissions `0600`; leaving its field blank preserves the saved password.
Use **Clear saved SMTP password** to remove it. This local runtime file is ignored
by Git. No email is sent when saving settings.

The **Email addresses and templates** table stores an alias, email address, and
notification body template per recipient. Disabled demo rows are examples only.
Supported placeholders are `{alias}`, `{error}`, `{source}`, `{detected}`,
`{case_number}`, and `{trace}`. A blank template uses the demo template.
Email groups accept aliases or email addresses; aliases are resolved when the group
is saved. Update the group after changing a recipient address.

When no SMTP settings have been saved, the monitor uses these environment variables:

- `LOGENT_SMTP_HOST`: SMTP hostname
- `LOGENT_SMTP_FROM`: sender email address
- `LOGENT_SMTP_PORT`: default 587 for STARTTLS, 465 for SSL
- `LOGENT_SMTP_SECURITY`: `starttls` (default), `ssl`, or `plain`
- `LOGENT_SMTP_USER` and `LOGENT_SMTP_PASSWORD`: optional authentication

Restart the monitor after installing this code or changing SMTP environment
variables. Saved SMTP settings, templates, table settings and email groups are
read for every incident.
