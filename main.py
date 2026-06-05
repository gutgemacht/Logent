#!/usr/bin/env python3
import sys
import logging  # Add this import
from rich.console import Console
import config
from core.orchestrator import Orchestrator

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.FileHandler("log_monitor.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

_console = Console()

def main() -> None:
    logger = logging.getLogger(__name__)
    try:
        logger.info("Starting log monitor...")
        orchestrator = Orchestrator()
        orchestrator.run()
    except FileNotFoundError as exc:
        _console.print(f"\n[bold red]Configuration error:[/bold red] {exc}\n")
        logger.error(f"Configuration error: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        pass


# Добавьте этот код в main.py
logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger(__name__)

def main() -> None:
    _logger.info("Starting log monitor...")
    try:
        orchestrator = Orchestrator()
        orchestrator.run()
    except FileNotFoundError as exc:
        print(f"\n[bold red]Configuration error:[/bold red] {exc}\n")
        _logger.error(f"Configuration error: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        _logger.info("Log monitor stopped.")    
