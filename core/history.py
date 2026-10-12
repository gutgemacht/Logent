"""Local Chroma history. Statistics are derived from immutable event records."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from core.error_context import compatible_errors, validate_solution_context, history_difference, problem_kind, symptom_terms, reported_paths
from core.system_context import system_context
from core.knowledge import SCHEMA_VERSION, redact, template_commands, render_commands


def normalize_error(line: str) -> str:
    # Keep paths, error codes and other meaningful numbers intact.
    line = re.sub(r'^\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:\s?[+-]\d{2}:?\d{2}|Z)?\]?\s*', '', line)
    line = re.sub(r'\b(pid\s*[=:]\s*)\d+\b', r'\1<PID>', line, flags=re.I)
    line = re.sub(r'\b([A-Za-z_][\w.-]*)\[\d+\]:', r'\1[<PID>]:', line)
    return redact(' '.join(line.split()))


def error_key(line: str) -> str:
    return hashlib.sha256(normalize_error(line).encode()).hexdigest()


def _rows(result: dict) -> list[dict]:
    return [dict(json.loads(doc), id=id_) for id_, doc in zip(result['ids'], result['documents'])]


class HistoryStore:
    def __init__(self, path: str, embedding_function=None, context: dict | None = None) -> None:
        import chromadb
        from chromadb.config import Settings
        if embedding_function is None:
            from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
            embedding_function = ONNXMiniLM_L6_V2(preferred_providers=['CPUExecutionProvider'])
            embedding_function.DOWNLOAD_PATH = Path(path).parent / '.chroma_models' / 'all-MiniLM-L6-v2'
        self.context = context or system_context()
        self.machine_id = self.context["machine_id"]
        self.environment_id = hashlib.sha256(json.dumps({field: self.context.get(field) for field in ('machine_id', 'os', 'uid')}, sort_keys=True).encode()).hexdigest()[:24]
        self.embedding_function = embedding_function
        self.client = chromadb.PersistentClient(path=path, settings=Settings(anonymized_telemetry=False))
        self.errors = self.client.get_or_create_collection('logent_errors', embedding_function=embedding_function)
        self.occurrences = self.client.get_or_create_collection('logent_occurrences', embedding_function=None)
        self.solutions = self.client.get_or_create_collection('logent_solutions', embedding_function=None)
        self.attempts = self.client.get_or_create_collection('logent_attempts', embedding_function=None)
        self.investigations = self.client.get_or_create_collection('logent_investigations', embedding_function=None)
        self.playbooks = self.client.get_or_create_collection('logent_diagnostics', embedding_function=embedding_function)
        self.linux_library = self.client.get_or_create_collection('logent_linux_library', embedding_function=None)
        self.search_index = self.client.get_or_create_collection('logent_search_index', embedding_function=embedding_function)
        self.seed_linux_library()

    def search_problems(self, query, limit=10, *, eligible_ids=None):
        from core.history_search import rank
        records = self.errors.get(include=['documents', 'metadatas'])
        allowed = set(eligible_ids) if eligible_ids is not None else None
        solutions = _rows(self.solutions.get(include=['documents']))
        grouped = {}
        for solution in solutions:
            if solution.get('obsolete'):
                continue
            grouped.setdefault(solution['error_key'], []).append(solution)
        candidates = []
        for id_, error, metadata in zip(records['ids'], records['documents'], records['metadatas']):
            if allowed is not None and id_ not in allowed:
                continue
            text = [error]
            for solution in grouped.get(id_, []):
                try:
                    validate_solution_context(error, solution.get('commands', []))
                except ValueError:
                    continue
                text.extend([solution.get('notes', ''), *solution.get('manual_steps', []),
                             *(item['command'] for item in solution.get('commands', []))])
            candidates.append(dict(id=id_, error=error, metadata=metadata or {}, search_text='\n'.join(text)))
        return rank(self.search_index, candidates, query, 'problem', limit)

    def search_linux_references(self, query, limit=25, *, eligible_ids=None):
        from core.history_search import rank
        allowed = set(eligible_ids) if eligible_ids is not None else None
        rows = self.list_linux_library()
        candidates = [dict(row, error=row['symptom'], search_text='\n'.join([row['title'], row['symptom'], *row['guidance']]))
                      for row in rows if allowed is None or row['id'] in allowed]
        return rank(self.search_index, candidates, query, 'reference', limit)

    def deletion_preview(self, *, problem_id=None, case_id=None):
        from core.history_deletion import preview
        return preview(self, problem_id=problem_id, case_id=case_id)

    def delete_problem(self, id_):
        from core.history_deletion import delete
        counts = delete(self, problem_id=id_)
        from core.problem_actions import collection
        collection(self).delete(ids=['problem-' + id_])
        return counts

    def delete_case(self, id_):
        from core.history_deletion import delete
        return delete(self, case_id=id_)

    def seed_linux_library(self):
        """Add versioned references once. Preserve deactivations and all user history."""
        from core.linux_library import ENTRIES
        existing = set(self.linux_library.get(include=[])['ids'])
        rows = [item for item in ENTRIES if item['id'] not in existing]
        if rows:
            self.linux_library.upsert(ids=[item['id'] for item in rows],
                documents=[json.dumps(item) for item in rows], embeddings=[[0.0] for item in rows],
                metadatas=[{'category': item['category'], 'source': item['source'], 'version': item['version']} for item in rows])
        return len(rows)

    def list_linux_library(self):
        return _rows(self.linux_library.get(include=['documents']))

    def set_linux_reference_active(self, id_, active):
        rows = _rows(self.linux_library.get(ids=[id_], include=['documents']))
        if not rows:
            raise ValueError('Linux reference not found')
        row = rows[0]
        row['active'] = bool(active)
        self.linux_library.update(ids=[id_], documents=[json.dumps(row)], embeddings=[[0.0]])

    def linux_diagnostic_plan(self, error):
        from core.linux_library import match_entry, build_plan
        matched = match_entry(self.list_linux_library(), error, self.context)
        if matched:
            plan = build_plan(matched, error, self.context)
            if plan:
                return plan
        # Exact saved plans may avoid regeneration. A similar vector is never
        # enough to execute commands for another target or environment.
        from core.diagnostic_commands import validate_diagnostic_commands
        for item in self.list_playbooks():
            if (not item.get('active') or normalize_error(item['symptom']) != normalize_error(error)
                    or item.get('environment', {}) != self.context):
                continue
            paths = reported_paths(error)
            try:
                commands = render_commands(item['commands'], paths[0] if len(paths) == 1 else None)
                validate_diagnostic_commands(commands)
            except ValueError:
                continue
            # Saved criteria remain hypotheses unless validated separately in
            # the existing trusted-criteria workflow.
            return dict(hypothesis=item['title'], commands=commands, verification_checks=[],
                        library_id=item['id'], library_category='saved-diagnostic-scenario',
                        sources=[], source=item.get('source', 'agent'))
        return None

    def record_occurrence(self, event) -> tuple[str, str]:
        key = hashlib.sha256((self.environment_id + ":" + normalize_error(event.line)).encode()).hexdigest()
        self.errors.upsert(ids=[key], documents=[normalize_error(event.line)], metadatas=[{"machine_id": self.machine_id, "environment_id": self.environment_id,
            'hostname': str(self.context.get('hostname', '')), 'os': str(self.context.get('os', '')), 'user': str(self.context.get('user', ''))}])
        occurrence = str(uuid4())
        record = {'error_key': key, 'line': event.line, 'filepath': event.filepath, 'timestamp': event.timestamp, 'machine_id': self.machine_id,
                  'triggers': list(getattr(event, 'triggers', ())), 'environment': self.context, 'schema_version': SCHEMA_VERSION}
        self.occurrences.add(ids=[occurrence], documents=[json.dumps(redact(record))], metadatas=[{'error_key': key, 'machine_id': self.machine_id, 'environment_id': self.environment_id}], embeddings=[[0.0]])
        return key, occurrence

    def save_solution(self, key: str, commands: list[dict], notes: str, source: str, conditions: dict | None = None, verification_commands: list[dict] | None = None, *, diagnosis=None, manual_steps=None, verification_checks=None) -> str:
        payload = {'commands': commands, 'notes': notes, 'conditions': conditions or {}, 'verification_commands': verification_commands or [], 'diagnosis': diagnosis or {}, 'manual_steps': manual_steps or [], 'verification_checks': verification_checks or [], 'schema_version': SCHEMA_VERSION}
        solution_id = hashlib.sha256((key + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
        existing = self.solutions.get(ids=[solution_id])
        if not existing['ids']:
            record = dict(payload, error_key=key, source=source, machine_id=self.machine_id, obsolete=False, created_at=datetime.now().astimezone().isoformat())
            self.solutions.add(ids=[solution_id], documents=[json.dumps(redact(record))], metadatas=[{'error_key': key, 'machine_id': self.machine_id, 'environment_id': self.environment_id}], embeddings=[[0.0]])
        return solution_id

    def record_attempt(self, key: str, occurrence: str, solution_id: str | None, outcome: str,
                       execution=None, *, case_id=None, verification=None, attempt_id=None, occurrence_ids=None):
        # Old callers skipping feedback do not create a rating. Actual applications
        # provide an attempt_id and remain recorded even without feedback.
        if outcome == 'skipped' and not attempt_id:
            return
        if outcome not in {'yes', 'no', 'unsure', 'skipped', 'not_reported'}:
            raise ValueError('Invalid solution outcome')
        id_ = attempt_id or (hashlib.sha256((case_id + ':' + str(solution_id)).encode()).hexdigest() if case_id else str(uuid4()))
        previous = _rows(self.attempts.get(ids=[id_]))
        record = previous[0] if previous else {'started': datetime.now().astimezone().isoformat(), 'feedback_history': []}
        record.pop('id', None)
        if record.get('outcome') != outcome and outcome in {'yes', 'no', 'unsure'}:
            record['feedback_history'].append({'outcome': outcome, 'timestamp': datetime.now().astimezone().isoformat()})
        record.update(error_key=key, occurrence_id=occurrence, occurrence_ids=occurrence_ids or [occurrence],
                      solution_id=solution_id, outcome=outcome, execution=execution or [],
                      timestamp=datetime.now().astimezone().isoformat(), schema_version=SCHEMA_VERSION, attempt_id=id_)
        if case_id:
            record['case_id'] = case_id
        if verification:
            record['verification'] = verification
        self.attempts.upsert(ids=[id_], documents=[json.dumps(redact(record))],
                             metadatas=[{'error_key': key, 'machine_id': self.machine_id, 'environment_id': self.environment_id}], embeddings=[[0.0]])
        return id_

    def update_feedback(self, attempt_id, outcome):
        rows = _rows(self.attempts.get(ids=[attempt_id]))
        if not rows:
            raise ValueError('Attempt not found')
        if outcome == 'skipped':
            return
        row = rows[0]
        updated = self.record_attempt(row['error_key'], row['occurrence_id'], row['solution_id'], outcome,
            row.get('execution'), case_id=row.get('case_id'), verification=row.get('verification'),
            attempt_id=attempt_id, occurrence_ids=row.get('occurrence_ids'))
        if row.get('case_id'):
            cases = _rows(self.investigations.get(ids=[row['case_id']]))
            if cases and cases[0].get('attempt_id') == attempt_id and cases[0].get('feedback_scope') != 'custom_solution':
                from core.reporting import SessionReport
                case = cases[0]
                report = SessionReport.__new__(SessionReport)
                report.data = case
                ended = case.get('ended')
                report.add('feedback_corrected', {'attempt_id':attempt_id, 'outcome':outcome})
                case['outcome'] = outcome
                report.finish()
                if ended:
                    case['ended'] = ended
                self.save_investigation(case)
        return updated

    def summary(self, key: str) -> dict:
        occurrences = self.occurrences.get(where={'error_key': key})['ids']
        raw_attempts = _rows(self.attempts.get(where={'error_key': key}))
        unique = {}
        for attempt in sorted(raw_attempts, key=lambda item: item['timestamp']):
            identity = attempt.get('attempt_id') or ((attempt['case_id'], attempt['solution_id']) if attempt.get('case_id') else attempt['id'])
            unique[identity] = attempt
        attempts = list(unique.values())
        solutions = _rows(self.solutions.get(where={'error_key': key}))
        investigations = _rows(self.investigations.get(where={'error_key': key}))
        recurrence_count = sum(bool(case.get('recurrence_of_case')) for case in investigations)
        stored_error = self.errors.get(ids=[key])['documents']
        for solution in solutions:
            solution['context_valid'] = True
            if stored_error:
                try:
                    validate_solution_context(stored_error[0], solution.get('commands', []))
                except ValueError as exc:
                    solution['context_valid'] = False
                    solution['context_reason'] = str(exc)
            results = [a['outcome'] for a in attempts if a['solution_id'] == solution['id']]
            solution['last_worked'] = max((a['timestamp'] for a in attempts if a['solution_id'] == solution['id'] and a['outcome'] == 'yes'), default=None)
            verified_times = [case.get('ended', case['started']) for case in investigations
                              if case.get('verification_solution_id') == solution['id'] and case.get('verification', {}).get('status') == 'disproved']
            verified_times += [attempt['timestamp'] for attempt in attempts if not attempt.get('case_id') and attempt['solution_id'] == solution['id'] and attempt.get('verification', {}).get('status') == 'disproved']
            solution['last_verified'] = max(verified_times, default=None)
            solution['verified_successes'] = len(verified_times)
            solution['helped'] = results.count('yes')
            solution['failed'] = results.count('no')
            solution['unsure'] = results.count('unsure')
            rated = solution['helped'] + solution['failed']
            solution['success_rate'] = solution['helped'] / rated if rated else None
        solutions.sort(key=lambda s: (s['helped'], s['success_rate'] if s['success_rate'] is not None else -1, -s['failed']), reverse=True)
        solved = {id_ for a in attempts if a['outcome'] == 'yes' for id_ in a.get('occurrence_ids', [a['occurrence_id']])}
        latest = {}
        for attempt in sorted(attempts, key=lambda a: a['timestamp']):
            for id_ in attempt.get('occurrence_ids', [attempt['occurrence_id']]):
                latest[id_] = attempt['outcome']
        failed = {id_ for id_, outcome in latest.items() if outcome == 'no' and id_ not in solved}
        unsure = {id_ for id_, outcome in latest.items() if outcome == 'unsure' and id_ not in solved}
        saved_ids = {solution['id'] for solution in solutions}
        solved_with_saved = {id_ for a in attempts if a['outcome'] == 'yes' and a['solution_id'] in saved_ids
                             for id_ in a.get('occurrence_ids', [a['occurrence_id']])}
        return {'seen': len(occurrences), 'solved': len(solved), 'failed': len(failed),
                'unsure': len(unsure), 'unreported': len(set(occurrences) - solved - failed - unsure),
                'solved_without_saved_solution': len(solved - solved_with_saved), 'solutions': solutions,
                'incidents': len(investigations), 'attempts': len(attempts), 'recurrences':recurrence_count,
                'rated_attempts': sum(a['outcome'] in {'yes', 'no'} for a in attempts)}

    def related(self, line: str, key: str, limit: int = 3, *, environment_id=None, include_other_environments=False) -> list[dict]:
        count = self.errors.count()
        if count <= 1:
            return []
        result = self.errors.query(query_texts=[normalize_error(line)], n_results=min(count, max(limit + 1, 20)),
            **({} if include_other_environments else {'where': {'environment_id': environment_id or self.environment_id}}))
        matches = [{'error': document, 'key': id_, 'distance': distance, 'summary': self.summary(id_),
                 'environment': metadata or {},
                 'match_reason': ('Same saved environment' if (metadata or {}).get('environment_id') == (environment_id or self.environment_id) else 'Other environment — reference only; verify application and OS compatibility') + '; compatible symptom type: ' + problem_kind(line).replace('_', ' ') + '. Historical cause still requires fresh verification.',
                 'difference': history_difference(line, document)}
                for id_, document, distance, metadata in zip(result['ids'][0], result['documents'][0], result['distances'][0], result['metadatas'][0])
                if id_ != key and distance <= (0.5 if problem_kind(line) == 'unknown' else 1.0) and compatible_errors(line, document)]
        anchors = symptom_terms(line)
        matches.sort(key=lambda item: (-len(anchors & symptom_terms(item['error'])), item['distance']))
        return matches[:limit]

    def list_solutions(self) -> list[dict]:
        # Include legacy entries for explicit management, never automatic reuse.
        entries = _rows(self.solutions.get())
        summaries = {}
        for entry in entries:
            key = entry['error_key']
            if key not in summaries:
                summaries[key] = {item['id']: item for item in self.summary(key)['solutions']}
            entry.update(summaries[key].get(entry['id'], {}))
        return entries

    def get_solution(self, solution_id: str) -> dict:
        rows = _rows(self.solutions.get(ids=[solution_id]))
        if not rows:
            raise ValueError("Solution not found")
        return rows[0]

    def set_obsolete(self, solution_id: str, obsolete: bool = True) -> None:
        record = self.get_solution(solution_id)
        record.pop('id')
        record['obsolete'] = obsolete
        self.solutions.update(ids=[solution_id], documents=[json.dumps(record)], embeddings=[[0.0]])

    def edit_solution(self, solution_id: str, commands: list[dict], notes: str, **details) -> str:
        previous = self.get_solution(solution_id)
        # Edited commands do not inherit the previous version's success rating.
        preserved = {field: previous.get(field) for field in ('conditions', 'verification_commands', 'diagnosis', 'manual_steps', 'verification_checks')}
        preserved.update(details)
        new_id = self.save_solution(previous['error_key'], commands, notes, 'user-edited', **preserved)
        if new_id != solution_id:
            self.set_obsolete(solution_id)
        return new_id

    def delete_solution(self, solution_id: str) -> None:
        self.get_solution(solution_id)
        self.solutions.delete(ids=[solution_id])

    def save_investigation(self, data: dict) -> None:
        metadata = {'environment_id': self.environment_id, 'machine_id': self.machine_id,
                    'error_key': data.get('error_key') or '', 'case_number': data['case_number'],
                    'status': data['status'], 'started': data['started']}
        self.investigations.upsert(ids=[data['id']], documents=[json.dumps(redact(data))], metadatas=[metadata], embeddings=[[0.0]])

    def case_records(self, key=None) -> list[dict]:
        result = self.investigations.get(**({'where': {'error_key': key}} if key else {}), include=['documents'])
        return sorted(_rows(result), key=lambda case: case['started'], reverse=True)

    def problem_details(self, key):
        return {'occurrences': _rows(self.occurrences.get(where={'error_key': key}, include=['documents'])),
                'attempts': _rows(self.attempts.get(where={'error_key': key}, include=['documents'])),
                'investigations': self.case_records(key)}

    def save_playbook(self, error, plan, verdict, *, case_id=None, source='agent', application='', application_version=''):
        from core.error_context import problem_kind
        commands = template_commands(plan['commands'], error)
        # Validate concrete commands before creating the reusable template.
        from core.diagnostic_commands import validate_diagnostic_commands
        validate_diagnostic_commands(plan['commands'])
        payload = dict(title=plan['hypothesis'], symptom=normalize_error(error), category=problem_kind(error),
                       commands=commands, verification_checks=plan.get('verification_checks', []),
                       diagnosis=verdict, environment=self.context, application=application,
                       application_version=application_version, source=source, schema_version=SCHEMA_VERSION)
        id_ = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        prior = self.playbooks.get(ids=[id_])
        previous = json.loads(prior['documents'][0].split('\n', 1)[1]) if prior['ids'] else {}
        payload.update(created=previous.get('created') or datetime.now().astimezone().isoformat(),
                       updated=datetime.now().astimezone().isoformat(), active=previous.get('active', True),
                       case_ids=list(dict.fromkeys(previous.get('case_ids', []) + ([case_id] if case_id else []))))
        self.playbooks.upsert(ids=[id_], documents=[normalize_error(error) + '\n' + json.dumps(redact(payload))],
                              embeddings=self.embedding_function([redact(normalize_error(error) + '\n' + payload['title'] + '\n' + payload['category'])]),
                              metadatas=[{'category': payload['category'], 'machine_id': self.machine_id, 'os': self.context['os']}])
        return id_

    def list_playbooks(self):
        rows = self.playbooks.get(include=['documents'])
        return [dict(json.loads(doc.split('\n', 1)[1]), id=id_) for id_, doc in zip(rows['ids'], rows['documents'])]

    def set_playbook_active(self, id_, active):
        row = next((item for item in self.list_playbooks() if item['id'] == id_), None)
        if row is None:
            raise ValueError('Diagnostic scenario not found')
        row.pop('id')
        row['active'] = bool(active)
        self.playbooks.update(ids=[id_], documents=[row['symptom'] + '\n' + json.dumps(redact(row))],
                              embeddings=self.embedding_function([row['symptom'] + '\n' + row['title'] + '\n' + row['category']]))

    def diagnostic_candidates(self, error, limit=3):
        from core.error_context import problem_kind
        if not self.playbooks.count():
            return []
        # Classify stored text again so scenarios saved under an older/unknown
        # category remain usable without rewriting the user's database.
        result = self.playbooks.query(query_texts=[normalize_error(error)], n_results=min(20, self.playbooks.count()))
        rows = [dict(json.loads(doc.split('\n', 1)[1]), id=id_, distance=distance)
                for id_, doc, distance in zip(result['ids'][0], result['documents'][0], result['distances'][0])]
        for item in rows:
            item['category'] = problem_kind(item['symptom'])
        return [item for item in rows if item.get('active') and item['distance'] <= 1.0
                and compatible_errors(error, item['symptom'])][:limit]
