"""Distinguish reported problems before retrieving history or proposing fixes."""
import re

NETWORK_KINDS = frozenset({'connection_timeout', 'connection_refused', 'dns_resolution', 'address_in_use'})


def problem_kind(line: str) -> str:
    if re.search(r'out[- ]of[- ](?:ram[- ]+)?memory|oom[-_ ]kill|cannot allocate memory|\benomem\b|\b(?:no(?: free)?|low|insufficient|less|not enough)\s+(?:ram|memory)\b|\bhigh memory consumption\b', line, re.I):
        return 'memory_exhaustion'
    if re.search(r'kernel panic|\boops\b|\b(?:soft|hard)[ -]?lockup\b|kernel.*(?:BUG:|NULL pointer|Call Trace)', line, re.I):
        return 'kernel_fault'
    if re.search(r'I/O error|filesystem corruption|read-only file system|medium error', line, re.I):
        return 'storage_fault'
    if re.search(r'machine check|hardware error|uncorrectable|overheat|thermal trip', line, re.I):
        return 'hardware_fault'
    if re.search(r'address already in use|\beaddrinuse\b', line, re.I):
        return 'address_in_use'
    if re.search(r'connection refused|\beconnrefused\b', line, re.I):
        return 'connection_refused'
    if re.search(r'failed to resolve|could not resolve|name resolution|hostname.*(?:not found|resolve)|\bnxdomain\b|\beai_again\b', line, re.I):
        return 'dns_resolution'
    if re.search(r'timed?\s*out|timeout|\betimedout\b', line, re.I) and re.search(r'connect|network|socket|request|https?\b', line, re.I):
        return 'connection_timeout'
    if re.search(r'no space left|disk full|\benospc\b', line, re.I):
        return 'disk_full'
    if re.search(r'\bunit\b.*not found|\.service.*(?:not found|does not exist)', line, re.I):
        return 'missing_service'
    if re.search(r'\bnot installed\b|\bcommand not found\b|\b(?:program|executable|binary|command)\b.*\b(?:not found|missing|unavailable)\b', line, re.I) or (reported_programs(line) and not reported_paths(line) and not re.search(r'\b(?:file|directory|path|unit|host(?:name)?|service)\b', line, re.I)):
        return 'missing_executable'
    missing = re.search(r'no such file|not found|not present(?:ed)?|does not exist|doesn.t exist|not exist|\bmissing\b|\babsent\b', line, re.I)
    permissions = re.search(r'permission|access denied|\beacces\b|\beperm\b', line, re.I)
    if missing and not permissions:
        return 'missing_path'
    if permissions and not missing:
        return 'permissions'
    if re.search(r'main process exited|failed with result|start request repeated|dependency failed', line, re.I):
        return 'service_failure'
    if re.search(r'traceback|exception|\b[A-Za-z][\w.]*(?:Error|Exception)\b', line):
        return 'application_exception'
    return 'unknown'


def history_difference(current, previous):
    differences = []
    if reported_programs(current) != reported_programs(previous):
        differences.append('Programs: current ' + (', '.join(reported_programs(current)) or 'not specified')
                           + '; saved ' + (', '.join(reported_programs(previous)) or 'not specified'))
    for title, expression in [('Paths', r'(?<!\w)/[\w./-]+'),
                              ('Endpoints', r'\b(?:[\w.-]+):\d+\b'),
                              ('Reported codes', r'\b(?:code|status|errno)[=: ]+\w+')]:
        before = sorted(set(re.findall(expression, previous, re.I)))
        now = sorted(set(re.findall(expression, current, re.I)))
        if before != now:
            differences.append(f"{title}: current {', '.join(now) or 'not specified'}; saved {', '.join(before) or 'not specified'}")
    return '; '.join(differences) or 'The error wording differs. No path, endpoint or code difference was extracted; inspect the full messages and fresh evidence.'


def compatible_errors(current: str, previous: str) -> bool:
    # Unknown is not a wildcard: lexical similarity does not establish a cause.
    kind = problem_kind(current)
    if kind != problem_kind(previous):
        return False
    if kind != 'unknown':
        return True
    a, b = symptom_terms(current), symptom_terms(previous)
    return len(a & b) >= 2 and len(a & b) / max(1, len(a | b)) >= 0.4


def symptom_terms(line):
    generic = set('error errors warning warn err critical fatal failed failure problem issue occurred system now fix it the a an for of to is in on and or with file pid process not no unknown detected found installed missing unavailable'.split())
    return {term.casefold() for term in re.findall(r'[\w./-]+', line)
            if not term.isdigit() and term.casefold() not in generic and len(term) > 1}


def reported_programs(line):
    generic = {'file', 'directory', 'path', 'unit', 'service', 'hostname', 'host', 'error', 'warning', 'command', 'program', 'executable', 'binary'}
    names = re.findall(r'\b([A-Za-z0-9][\w.+-]*)\s*(?::\s*|\s+(?:is\s+)?)(?:not installed|(?:command )?not found)\b', line, re.I)
    return sorted({name for name in names if name.casefold() not in generic})


def reported_paths(line: str) -> list[str]:
    line = re.sub(r'\b[a-z][\w+.-]*://\S+', '', line, flags=re.I)
    return list(dict.fromkeys(re.findall(r'(?<!\w)/[\w/-](?:[\w./-]*[\w/-])?', line)))


def validate_solution_context(error: str, commands: list[dict]) -> None:
    kind = problem_kind(error)
    for command in commands:
        if re.search(r'\b(?:chmod|chown|chgrp|setfacl)\b', command.get('command', '')):
            if kind == 'missing_path':
                raise ValueError('A permissions change cannot fix a missing file. Verify existence and diagnose restoration first.')
            if kind in NETWORK_KINDS:
                raise ValueError(f'File permission changes are not an applicable fix for {kind.replace("_", " ")}. Diagnose the endpoint, listener, route and DNS instead.')
