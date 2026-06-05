#!/usr/bin/env python3
"""
main.py
=======
Multi-Agent Log Monitor – entry point.

Usage
-----
    python main.py

Prerequisites
-------------
    pip install rich watchdog

    Edit ``config.py`` to set your Ollama host/model.
    Edit ``logs.lst`` to list the files/directories to monitor.
"""

import sys

from rich.console import Console

import config  # noqa: F401  (imported to validate it exists)
from core.orchestrator import Orchestrator

_console = Console()


def main() -> None:
    try:
        orchestrator = Orchestrator()
        orchestrator.run()
    except FileNotFoundError as exc:
        _console.print(f"\n[bold red]Configuration error:[/bold red] {exc}\n")
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
