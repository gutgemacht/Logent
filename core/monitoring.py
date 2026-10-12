"""Local monitor heartbeats and event traces consumed by the web dashboard."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import tempfile
from core.knowledge import redact


def source_status(paths):
    rows = []
    for name in paths:
        path = Path(name)
        exists = path.exists()
        readable = exists and os.access(path, os.R_OK)
        rows.append({'Path': str(path), 'Type': 'Directory' if path.is_dir() else 'File',
                     'Status': 'Readable' if readable else 'Unreadable' if exists else 'Missing'})
    return rows


class MonitorState:
    def __init__(self, directory, paths, rules_path, queue):
        self.directory = Path(directory)
        self.queue = queue
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.data = {'pid': os.getpid(), 'started': datetime.now(timezone.utc).isoformat(),
                     'state': 'Starting', 'stage': 'Monitoring', 'paths': [str(path) for path in paths],
                     'rules_path': str(rules_path), 'detected_count': 0, 'ignored_count': 0,
                     'recent_events': [], 'current_case': None, 'pending': []}
        self.path = self.directory / f'{os.getpid()}.json'
        self.rules_provider = None

    def update(self, **values):
        with self.lock:
            self.data.update(values)

    def detected(self, event, ignored=False):
        with self.lock:
            self.data['ignored_count' if ignored else 'detected_count'] += 1
            self.data['last_event'] = event
            self.data['recent_events'] = (self.data['recent_events'] + [dict(event, ignored=ignored)])[-100:]

    def flush(self):
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.data['heartbeat'] = time.time()
            self.data['queued_count'] = self.queue.qsize()
            self.data['sources'] = source_status(self.data['paths'])
            if self.rules_provider:
                self.data['active_rules'] = self.rules_provider()
            with self.queue.mutex:
                queued = list(self.queue.queue)[:30]
            self.data['queued_events'] = [{'error': e.line, 'source': e.filepath, 'detected': e.timestamp} for e in queued]
            fd, temporary = tempfile.mkstemp(dir=self.directory, prefix='.state-')
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump(redact(self.data), stream)
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def start(self):
        self.update(state='Running')
        self.flush()
        def loop():
            while not self.stop_event.wait(2):
                try:
                    self.flush()
                except OSError:
                    pass
        self.worker = threading.Thread(target=loop, daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        if hasattr(self, 'worker'):
            self.worker.join(timeout=3)
        self.update(state='Stopped', current_case=None)
        try:
            self.flush()
        except OSError:
            pass


def monitor_snapshots(directory):
    result = []
    for path in Path(directory).glob('*.json'):
        try:
            data = json.loads(path.read_text())
            data['live'] = data.get('state') == 'Running' and time.time() - data.get('heartbeat', 0) < 10
            result.append(data)
        except (OSError, ValueError):
            continue
    return sorted(result, key=lambda item: item.get('heartbeat', 0), reverse=True)
