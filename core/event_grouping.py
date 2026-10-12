"""Group consecutive matching queued errors without dropping occurrences."""
from collections import deque
import queue
from datetime import datetime
from core.history import error_key


class EventInbox:
    def __init__(self, source):
        self.source = source
        self.pending = deque()

    @staticmethod
    def _same_incident(first, event, window_seconds=None):
        if (event.filepath, error_key(event.line)) != (first.filepath, error_key(first.line)):
            return False
        if window_seconds is not None:
            try:
                return abs((datetime.fromisoformat(event.timestamp) - datetime.fromisoformat(first.timestamp)).total_seconds()) <= window_seconds
            except (ValueError, TypeError):
                # Legacy events without a parseable time retain consecutive grouping.
                return event.timestamp == first.timestamp
        return True

    def next_batch(self, timeout=0.5, max_batch=100, group_window_seconds=None):
        first = self.pending.popleft() if self.pending else self.source.get(timeout=timeout)
        batch = [first]
        if group_window_seconds is not None:
            batch.extend(self.collect_repeats(first, group_window_seconds, max_batch - 1))
            return batch
        while len(batch) < max_batch:
            try:
                event = self.pending.popleft() if self.pending else self.source.get_nowait()
            except queue.Empty:
                break
            if (event.filepath, error_key(event.line)) == (first.filepath, error_key(first.line)):
                batch.append(event)
            else:
                self.pending.appendleft(event)
                break
        return batch

    def collect_repeats(self, first, window_seconds=300, limit=1000):
        # Bounded snapshot: different problems remain in their original order.
        waiting = list(self.pending)
        self.pending.clear()
        for _ in range(limit):
            try:
                waiting.append(self.source.get_nowait())
            except queue.Empty:
                break
        repeats = []
        for event in waiting:
            if len(repeats) < limit and self._same_incident(first, event, window_seconds):
                repeats.append(event)
            else:
                self.pending.append(event)
        return repeats
