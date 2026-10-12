"""Reserve terminal rows for the current error while dialogs scroll below."""
from contextlib import contextmanager
import os
import threading
from io import StringIO

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ui.terminal_menu import supported


def error_header(event, state=None):
    body = Text()
    body.append(event.line, style='bold red')
    body.append(f'\nSource: {event.filepath} | Detected: {event.timestamp}', style='dim')
    if state:
        from core.reporting import PIPELINE
        index = PIPELINE.index(state['stage']) + 1 if state['stage'] in PIPELINE else 0
        body.append(f"\nPipeline {index}/{len(PIPELINE)}: {state['stage']} · {state['conclusion']}", style='bold cyan')
        body.append('\nCause: ' + state['cause'][:120], style='dim')
        from core.knowledge import SYMPTOMS, CAUSES
        body.append('\nSymptom: ' + SYMPTOMS.get(state.get('symptom_assessment', {}).get('status'), 'Not checked')
                    + ' | Cause: ' + CAUSES.get(state.get('cause_assessment', {}).get('status'), 'Not checked'), style='bold cyan')
        body.append('\nNext: ' + state['next_step'], style='dim')
    return Panel(body, title=state['case_number'] if state else 'Current error — investigation in progress', border_style='red', padding=(0, 1))


@contextmanager
def pinned_error(console, event, state=None):
    """Pin on VT-compatible interactive terminals; keep plain output unchanged.

    DEC scrolling margins keep stdout, stderr, Rich tables and prompts below
    the header. The original margins and automatic height are restored on exit.
    """
    if not supported(console) or console.legacy_windows:
        yield False
        return
    stop = threading.Event()
    original_height = console._height
    size = None
    active = False
    signature = None

    def refresh():
        nonlocal size, active, signature
        try:
            dimensions = os.get_terminal_size(console.file.fileno())
            width, height = dimensions.columns, dimensions.lines
        except OSError:
            width, height = console.width, original_height or 25
        if width < 20 or height < 10:
            # Tiny terminals cannot reserve a usable header and dialog area.
            if active:
                with console._lock:
                    console.file.write('\x1b[r\x1b[2J\x1b[H')
                    console.file.flush()
                console.height = original_height
                active = False
            size = (width, height)
            return
        current_signature = tuple(state.get(key) for key in ('stage', 'conclusion', 'cause', 'next_step')) if state else None
        changed_size = size != (width, height)
        if not changed_size and current_signature == signature:
            return
        signature = current_signature
        size = (width, height)
        stream = StringIO()
        renderer = Console(file=stream, width=width, force_terminal=True,
                           color_system=console.color_system, highlight=False)
        renderer.print(error_header(event, state))
        lines = stream.getvalue().splitlines()
        # Always retain room for choices. Unusually long errors are clipped visibly.
        limit = max(3, min(12, height // 2)) if state else max(3, height // 2)
        if len(lines) > limit:
            lines = lines[:limit - 1] + ['… error continues; full text is in the troubleshooting report']
        if state:
            lines += [''] * (limit - len(lines))
        rows = len(lines)
        with console._lock:
            if changed_size:
                console.file.write('\x1b[r\x1b[2J\x1b[H' + '\r\n'.join(lines))
                console.file.write(f'\x1b[{rows + 1};{height}r\x1b[{rows + 1};1H')
            else:
                # Preserve body, cursor and active prompts when only status changes.
                console.file.write('\x1b7\x1b[H' + '\r\n'.join('\x1b[2K' + line for line in lines) + '\x1b8')
            console.file.flush()
            console.height = height - rows
        active = True

    refresh()
    def watch_resize():
        while not stop.wait(0.2):
            refresh()
    worker = threading.Thread(target=watch_resize, daemon=True)
    worker.start()
    try:
        yield active
    finally:
        stop.set()
        worker.join(timeout=1)
        with console._lock:
            console.file.write('\x1b7\x1b[r\x1b8')
            console.file.flush()
            console.height = original_height
