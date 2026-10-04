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
import logging

from ui.display import TimestampConsole

# Set up logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S %z'
)

logger = logging.getLogger(__name__)

import config  # noqa: F401  (imported to validate it exists)
from core.orchestrator import Orchestrator

_console = TimestampConsole()

def main() -> None:
    try:
        logger.info("Starting Multi-Agent Log Monitor")
        orchestrator = Orchestrator()

        # Modify the log handler to filter out "Error" messages
        def log_filter(record):
            return "ERROR" not in record.getMessage().upper()

        logging.getLogger().addFilter(log_filter)

        orchestrator.run()
    except FileNotFoundError as exc:
        logger.error(f"Configuration error: {exc}")
        _console.print(f"\n[bold red]Configuration error:[/bold red] {exc}\n")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Script terminated by user (KeyboardInterrupt)")
        pass
    finally:
        logger.info("Multi-Agent Log Monitor has exited")

if __name__ == "__main__":
    main()
