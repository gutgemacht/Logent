"""Arrow-key menus using the standard library; Rich renders the status area."""
import os
import select
import sys
from datetime import datetime

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


def supported(console) -> bool:
    return os.name == 'posix' and sys.stdin.isatty() and console.is_terminal and not console.is_dumb_terminal and console.file is sys.stdout


def select_option(console, title: str, options: list[tuple[str, str]], status: str, default=None):
    import termios
    import tty
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    number_prefix = ""
    selected = next((i for i, (key, _) in enumerate(options) if key == default), 0)

    def render():
        body = Text()
        for index, (key, label) in enumerate(options):
            body.append(f"{'▶' if index == selected else ' '} [{key}] {label}\n", style='bold cyan' if index == selected else 'bold')
        stamp = datetime.now().astimezone().strftime('%H:%M:%S')
        return Group(Panel(body, title=title), Text(f'{stamp} | {status} | ↑/↓ choose · Enter confirm · number selects · Ctrl+C stop', style='dim'))

    try:
        termios.tcflush(fd, termios.TCIFLUSH)
        tty.setcbreak(fd)
        mode = termios.tcgetattr(fd)
        mode[3] &= ~termios.ISIG  # Read Ctrl+C explicitly, then restore on exit.
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        with Live(render(), console=console, refresh_per_second=8, transient=True) as live:
            while True:
                raw = os.read(fd, 1)
                if raw == b'':
                    raise EOFError
                if raw == b'\x03':
                    raise KeyboardInterrupt
                if raw == b'\x1b' and select.select([fd], [], [], 0.05)[0]:
                    code = os.read(fd, 2)
                    number_prefix = ''
                    if code == b'[A':
                        selected = (selected - 1) % len(options)
                    elif code == b'[B':
                        selected = (selected + 1) % len(options)
                elif raw in (b'\r', b'\n'):
                    return options[selected][0]
                else:
                    key = raw.decode(errors='ignore')
                    number_prefix = number_prefix + key if key.isdigit() else ''
                    if not any(value.startswith(number_prefix) for value, _ in options):
                        number_prefix = key if key.isdigit() else ''
                    for i, (value, _) in enumerate(options):
                        if number_prefix == value:
                            selected = i
                live.update(render())
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)
