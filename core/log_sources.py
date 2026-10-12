"""Read and atomically save monitored paths without executing any commands."""
import os
from pathlib import Path
import tempfile


def parse_sources(text, config_file):
    paths = []
    for line in text.splitlines():
        name = line.strip()
        if not name or name.startswith('#'):
            continue
        if '\x00' in name:
            raise ValueError('A log path contains an invalid null character.')
        path = Path(name).expanduser()
        if not path.is_absolute():
            path = Path(config_file).resolve().parent / path
        resolved = str(path.resolve())
        if resolved not in paths:
            paths.append(resolved)
    return paths


def save_sources(text, config_file):
    paths = parse_sources(text, config_file)
    if not paths:
        raise ValueError('Add at least one file or directory to monitor.')
    destination = Path(config_file)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix='.sources-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text.strip() + '\n')
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return paths
