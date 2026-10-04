"""Group consecutive matching queued errors without dropping occurrences."""
from collections import deque
import queue
from core.history import error_key


class EventInbox:
    def __init__(self, source):
        self.source = source
        self.pending = deque()

    def next_batch(self, timeout=0.5, max_batch=100):
        first = self.pending.popleft() if self.pending else self.source.get(timeout=timeout)
        batch = [first]
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
