"""Arrow-key menus using the standard library; Rich renders the status area."""
import os
import select
import sys
import re
from datetime import datetime

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


def supported(console) -> bool:
    return os.name == 'posix' and sys.stdin.isatty() and console.is_terminal and not console.is_dumb_terminal and console.file is sys.stdout


def read_key(fd):
    raw = os.read(fd, 1)
    if raw != b'\x1b':
        return raw
    code = b''
    while len(code) < 64 and select.select([fd], [], [], 0.05)[0]:
        code += os.read(fd, 1)
        if len(code) > 1 and 0x40 <= code[-1] <= 0x7e:
            break
    return b'\x1b' + code


def scroll_panel(console, renderable, offset, height):
    """Bound history to the available body rows, preserving Rich styling."""
    lines = console.render_lines(renderable, console.options.update(width=max(16, console.width - 3), height=None), pad=False)
    offset = max(0, min(offset, max(0, len(lines) - height)))
    thumb_size = max(1, round(height * min(1, height / max(1, len(lines)))))
    thumb = round((height - thumb_size) * offset / max(1, len(lines) - height))
    body = Text()
    for row in range(height):
        line = Text()
        if offset + row < len(lines):
            for segment in lines[offset + row]:
                line.append(segment.text, segment.style)
        line.truncate(max(16, console.width - 3), overflow='crop')
        line.pad_right(max(0, console.width - 3 - line.cell_len))
        line.append('█' if thumb <= row < thumb + thumb_size else '│', style='cyan')
        body.append_text(line)
        if row < height - 1:
            body.append('\n')
    title = f'History references · {offset + 1}–{min(offset + height, len(lines))} / {len(lines)}'
    return Panel(body, title=title, padding=0, border_style='blue'), offset, max(0, len(lines) - height)


def select_option(console, title: str, options: list[tuple[str, str]], status: str, default=None, *, preview=None):
    import termios
    import tty
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    number_prefix = ""
    selected = next((i for i, (key, _) in enumerate(options) if key == default), 0)
    scroll_offset = 0
    scroll_max = 0
    view_height = max(1, console.height - len(options) - 6)

    def render():
        nonlocal scroll_offset, scroll_max, view_height
        body = Text()
        for index, (key, label) in enumerate(options):
            if index:
                body.append('\n')
            body.append(f"{'▶' if index == selected else ' '} [{key}] {label}", style='bold cyan' if index == selected else 'bold')
        stamp = datetime.now().astimezone().strftime('%H:%M:%S')
        menu = Panel(body, title=title)
        hint = f'{stamp} | {status} | ↑/↓ choose · Enter confirm · Ctrl+C stop'
        if preview is None:
            return Group(menu, Text(hint, style='dim'))
        view_height = max(1, console.height - len(options) - 6)
        history, scroll_offset, scroll_max = scroll_panel(console, preview, scroll_offset, view_height)
        return Group(history, menu, Text('PgUp/PgDn · Home/End · mouse wheel: scroll history', style='dim'), Text(hint, style='dim'))

    try:
        termios.tcflush(fd, termios.TCIFLUSH)
        tty.setcbreak(fd)
        mode = termios.tcgetattr(fd)
        mode[3] &= ~termios.ISIG  # Read Ctrl+C explicitly, then restore on exit.
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        if preview is not None:
            console.file.write('\x1b[?1000h\x1b[?1006h')
            console.file.flush()
        with Live(render(), console=console, refresh_per_second=8, transient=True) as live:
            while True:
                raw = read_key(fd)
                if raw == b'':
                    raise EOFError
                if raw == b'\x03':
                    raise KeyboardInterrupt
                if raw.startswith(b'\x1b'):
                    code = raw[1:]
                    number_prefix = ''
                    if code == b'[A':
                        selected = (selected - 1) % len(options)
                    elif code == b'[B':
                        selected = (selected + 1) % len(options)
                    elif code == b'[5~':
                        scroll_offset = max(0, scroll_offset - view_height)
                    elif code == b'[6~':
                        scroll_offset = min(scroll_max, scroll_offset + view_height)
                    elif code in (b'[H', b'[1~', b'OH'):
                        scroll_offset = 0
                    elif code in (b'[F', b'[4~', b'OF'):
                        scroll_offset = scroll_max
                    elif preview is not None and (mouse := re.fullmatch(rb'\[<(64|65);\d+;\d+M', code)):
                        scroll_offset = max(0, min(scroll_max, scroll_offset + (-3 if mouse[1] == b'64' else 3)))
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
                live.update(render(), refresh=True)
    finally:
        if preview is not None:
            console.file.write('\x1b[?1000l\x1b[?1006l')
            console.file.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)
