"""Local Chroma history. Statistics are derived from immutable event records."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from core.error_context import compatible_errors
from core.system_context import system_context


def normalize_error(line: str) -> str:
    # Keep paths, error codes and other meaningful numbers intact.
    line = re.sub(r'^\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:\s?[+-]\d{2}:?\d{2}|Z)?\]?\s*', '', line)
    line = re.sub(r'\b(pid\s*[=:]\s*)\d+\b', r'\1<PID>', line, flags=re.I)
    line = re.sub(r'\b([A-Za-z_][\w.-]*)\[\d+\]:', r'\1[<PID>]:', line)
    return ' '.join(line.split())


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

    def record_occurrence(self, event) -> tuple[str, str]:
        key = hashlib.sha256((self.environment_id + ":" + normalize_error(event.line)).encode()).hexdigest()
        self.errors.upsert(ids=[key], documents=[normalize_error(event.line)], metadatas=[{"machine_id": self.machine_id, "environment_id": self.environment_id}])
        occurrence = str(uuid4())
        record = {'error_key': key, 'line': event.line, 'filepath': event.filepath, 'timestamp': event.timestamp, 'machine_id': self.machine_id}
        self.occurrences.add(ids=[occurrence], documents=[json.dumps(record)], metadatas=[{'error_key': key, 'machine_id': self.machine_id, 'environment_id': self.environment_id}], embeddings=[[0.0]])
        return key, occurrence

    def save_solution(self, key: str, commands: list[dict], notes: str, source: str, conditions: dict | None = None, verification_commands: list[dict] | None = None) -> str:
        payload = {'commands': commands, 'notes': notes, 'conditions': conditions or {}, 'verification_commands': verification_commands or []}
        solution_id = hashlib.sha256((key + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
        existing = self.solutions.get(ids=[solution_id])
        if not existing['ids']:
            record = dict(payload, error_key=key, source=source, machine_id=self.machine_id, obsolete=False, created_at=datetime.now().astimezone().isoformat())
            self.solutions.add(ids=[solution_id], documents=[json.dumps(record)], metadatas=[{'error_key': key, 'machine_id': self.machine_id, 'environment_id': self.environment_id}], embeddings=[[0.0]])
        return solution_id

    def record_attempt(self, key: str, occurrence: str, solution_id: str | None, outcome: str, execution: list[dict] | None = None) -> None:
        if outcome not in {'yes', 'no', 'unsure'}:
            raise ValueError('Invalid solution outcome')
        record = {'error_key': key, 'occurrence_id': occurrence, 'solution_id': solution_id,
                  'outcome': outcome, 'execution': execution or [], 'timestamp': datetime.now().astimezone().isoformat()}
        self.attempts.add(ids=[str(uuid4())], documents=[json.dumps(record)], metadatas=[{'error_key': key, 'machine_id': self.machine_id, 'environment_id': self.environment_id}], embeddings=[[0.0]])

    def summary(self, key: str) -> dict:
        occurrences = self.occurrences.get(where={'error_key': key})['ids']
        attempts = _rows(self.attempts.get(where={'error_key': key}))
        solutions = _rows(self.solutions.get(where={'error_key': key}))
        for solution in solutions:
            results = [a['outcome'] for a in attempts if a['solution_id'] == solution['id']]
            solution['last_worked'] = max((a['timestamp'] for a in attempts if a['solution_id'] == solution['id'] and a['outcome'] == 'yes'), default=None)
            solution['helped'] = results.count('yes')
            solution['failed'] = results.count('no')
            solution['unsure'] = results.count('unsure')
            rated = solution['helped'] + solution['failed']
            solution['success_rate'] = solution['helped'] / rated if rated else None
        solutions.sort(key=lambda s: (s['helped'], s['success_rate'] if s['success_rate'] is not None else -1, -s['failed']), reverse=True)
        return {'seen': len(occurrences), 'solved': len({a['occurrence_id'] for a in attempts if a['outcome'] == 'yes'}), 'solutions': solutions}

    def related(self, line: str, key: str, limit: int = 3) -> list[dict]:
        count = self.errors.count()
        if count <= 1:
            return []
        result = self.errors.query(query_texts=[normalize_error(line)], n_results=min(count, limit + 1), where={'environment_id': self.environment_id})
        return [{'error': document, 'key': id_, 'summary': self.summary(id_)}
                for id_, document, distance in zip(result['ids'][0], result['documents'][0], result['distances'][0])
                if id_ != key and distance <= 1.0 and compatible_errors(line, document)][:limit]

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

    def edit_solution(self, solution_id: str, commands: list[dict], notes: str) -> str:
        previous = self.get_solution(solution_id)
        # Edited commands do not inherit the previous version's success rating.
        new_id = self.save_solution(previous['error_key'], commands, notes, 'user-edited')
        if new_id != solution_id:
            self.set_obsolete(solution_id)
        return new_id

    def delete_solution(self, solution_id: str) -> None:
        self.get_solution(solution_id)
        self.solutions.delete(ids=[solution_id])
