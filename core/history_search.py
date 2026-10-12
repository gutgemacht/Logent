"""CPU hybrid retrieval for browsing knowledge; matches never confirm a diagnosis."""
from __future__ import annotations

import re
from core.error_context import problem_kind

# Domain vocabulary improves Linux queries without adding a model or LLM call.
ALIASES = {
    'memory_exhaustion': ('out of memory OOM insufficient RAM memory exhausted', r'\b(?:memory|ram|oom|arbeitsspeicher|speichermangel|памят\w*|озу)\b'),
    'permissions': ('permission denied access rights file mode ownership ACL', r'permission|access denied|chmod|chown|zugriff|berechtigung|dateirechte|доступ|прав[ао]\b'),
    'missing_path': ('missing file directory path file does not exist ENOENT', r'missing (?:file|path|directory)|file (?:missing|not found)|datei.*(?:fehlt|nicht gefunden)|нет файла|файл.*(?:отсутствует|не найден)'),
    'missing_executable': ('program executable command not found not installed package missing', r'not installed|command not found|(?:missing|absent) (?:program|command|package)|(?:program|software) missing|programm.*(?:fehlt|nicht installiert)|программ\w*.*(?:нет|отсутствует|не установл)'),
    'disk_full': ('disk full no space left filesystem block or inode capacity ENOSPC', r'disk full|no space|enospc|disk space|speicherplatz|festplatte.*voll|мест\w*.*диск|диск.*(?:полн|мест)'),
    'storage_fault': ('filesystem read only storage I/O error disk corruption', r'read.only|i/o|filesystem|dateisystem|файлов\w* систем|corrupt|beschädig'),
    'connection_timeout': ('network connection timeout request timed out ETIMEDOUT', r'timeout|timed out|zeitüberschreitung|тайм.?аут'),
    'connection_refused': ('network TCP connection refused ECONNREFUSED listener missing', r'connection refused|verbindung.*abgelehnt|соединени\w*.*отказ'),
    'dns_resolution': ('DNS hostname resolution failed resolver name lookup', r'\bdns\b|resolve|resolution|namensauflösung|резолв|разрешени\w*.*им'),
    'address_in_use': ('socket TCP address port already in use bind conflict', r'address.*in use|port.*(?:occupied|in use|belegt|занят)|адрес.*занят'),
    'missing_service': ('systemd unit service not found missing service', r'unit.*not found|missing service|dienst.*(?:fehlt|nicht gefunden)|служб\w*.*не найден'),
    'service_failure': ('systemd service failed exited restart dependency', r'service fail|systemd|dienst.*fehl|служб\w*.*(?:ошиб|сбой)'),
    'kernel_fault': ('kernel panic Oops watchdog lockup hung task RCU stall', r'kernel|lockup|watchdog|hung task|rcu|ядр\w*|зависан'),
    'hardware_fault': ('hardware machine check thermal overheating EDAC MCE', r'hardware|thermal|overheat|temperatur|оборудован|перегрев'),
    'application_exception': ('application exception traceback crash Python module import error', r'exception|traceback|crash|modulenotfound|исключени|absturz'),
    'resource_limits': ('too many open files file descriptors EMFILE ENFILE cannot fork task limit', r'too many open files|file descriptor|emfile|enfile|cannot fork|offene dateien|дескриптор'),
}


def query_kinds(text):
    found = {kind for kind, (_, pattern) in ALIASES.items() if re.search(pattern, text, re.I)}
    explicit = problem_kind(text)
    if explicit != 'unknown':
        found.add(explicit)
    return found


def record_kinds(text):
    explicit = problem_kind(text)
    # A structured symptom category wins over incidental terms in the log.
    if explicit != 'unknown':
        return {explicit}
    return query_kinds(text)


def terms(text):
    stop = {'error', 'errors', 'warning', 'failed', 'problem', 'the', 'is', 'a', 'of', 'to', 'for', 'with', 'and', 'or'}
    return {term for term in re.findall(r'[\w./-]+', text.casefold()) if len(term) > 1 and term not in stop}


def rank(index, candidates, query, record_type, limit=10):
    """Index current content, then rank only eligible candidates, not a pre-cut top K."""
    if not candidates or not query.strip() or limit < 1:
        return []
    kinds = query_kinds(query)
    prepared = []
    for candidate in candidates:
        candidate_kinds = record_kinds(candidate['error'])
        # Recognized symptom queries must not return unrelated error categories.
        if kinds and not (kinds & candidate_kinds):
            continue
        text = candidate['search_text']
        expanded = text + '\n' + ' '.join(ALIASES[kind][0] for kind in sorted(candidate_kinds) if kind in ALIASES)
        prepared.append((candidate, expanded))
    if not prepared:
        return []
    ids = [record_type + ':' + candidate['id'] for candidate, _ in prepared]
    current = index.get(ids=ids, include=['documents'])
    stored = dict(zip(current['ids'], current['documents']))
    changed = [(id_, text) for id_, (_, text) in zip(ids, prepared) if stored.get(id_) != text]
    if changed:
        index.upsert(ids=[id_ for id_, _ in changed], documents=[text for _, text in changed],
                     metadatas=[{'record_type':record_type, 'record_id':id_} for id_, _ in changed])
    expanded_query = query + '\n' + ' '.join(ALIASES[kind][0] for kind in sorted(kinds) if kind in ALIASES)
    result = index.query(query_texts=[expanded_query], n_results=len(ids),
                         where={'$and':[{'record_type':record_type}, {'record_id':{'$in':ids}}]},
                         include=['distances'])
    distances = dict(zip(result['ids'][0], result['distances'][0]))
    query_terms = terms(query)
    ranked = []
    for id_, (candidate, expanded) in zip(ids, prepared):
        distance = distances.get(id_)
        if distance is None:
            continue
        overlap = len(query_terms & terms(candidate['search_text'])) / max(1, len(query_terms))
        exact = query.casefold() in candidate['search_text'].casefold()
        # Unknown searches need semantic or literal evidence of relevance.
        if not kinds and not overlap and distance > 0.85:
            continue
        score = -distance + 0.45 * overlap + (0.5 if exact else 0)
        reasons = []
        if kinds:
            reasons.append('Compatible symptom: ' + ', '.join(sorted(kinds & record_kinds(candidate['error']))).replace('_', ' '))
        if overlap:
            reasons.append('Matching words in error or solution')
        reasons.append('CPU semantic similarity')
        ranked.append(dict(candidate, distance=distance, match_reason='; '.join(reasons), _score=score))
    ranked.sort(key=lambda row: (-row['_score'], row['distance'], row['id']))
    return [{key:value for key,value in row.items() if key not in {'_score', 'search_text'}} for row in ranked[:limit]]
