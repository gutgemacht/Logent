from contextlib import contextmanager
from datetime import datetime
from time import monotonic

from rich.live import Live
from rich.text import Text
import threading


@contextmanager
def activity(console, message: str):
    started = monotonic()
    stop = threading.Event()
    def text():
        stamp = datetime.now().astimezone().strftime('%H:%M:%S')
        return Text(f'{stamp} | {message} | {monotonic() - started:.0f}s elapsed | Ctrl+C cancels request', style='bold cyan')
    with Live(text(), console=console, refresh_per_second=4, transient=True) as live:
        def refresh():
            while not stop.wait(0.25):
                live.update(text())
        thread = threading.Thread(target=refresh, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1)
