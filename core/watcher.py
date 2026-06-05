"""
core/watcher.py
=============
Watchdog-based file system monitor that detects new log lines.
Spawns a thread to watch files/directories and queues detected errors.
"""  
from __future__ import annotations  
import logging
import re
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent
import queue
import threading
import time
from collections import OrderedDict

_logger = logging.getLogger("log_monitor.watcher")

class LogMonitorHandler(FileSystemEventHandler):
    """Callback for log file modifications."""
    
    def __init__(
        self,
        log_queue: queue.Queue,
        error_patterns: list[str],
        watched_files: list[str],
        watched_dirs: list[str]
    ) -> None:
        super().__init__()
        self._queue = log_queue
        self._patterns = [re.compile(p, re.IGNORECASE) for p in error_patterns]
        self._watched_files = watched_files
        self._watched_dirs = watched_dirs
        self._positions: OrderedDict[str, int] = OrderedDict()
        _logger.info(f"LogMonitorHandler initialized with {len(self._watched_files)} files")
    
    def _should_watch(self, path: str) -> bool:
        """Check if path is in watched list."""
        return str(path) in self._watched_files or any(
            str(path).startswith(d) for d in self._watched_dirs
        )
    
    def _eof_offset(self, path: str) -> int:
        """Get current end of file position."""
        try:
            return Path(path).stat().st_size
        except OSError:
            return 0
    
    def _scan_new_lines(self, filepath: str) -> None:
        """Scan for new log lines and queue errors."""
        if not Path(filepath).is_file():
            return
        pos = self._positions.get(filepath, 0)
        try:
            with open(filepath, "r", errors="ignore") as f:
                f.seek(pos)
                lines = f.readlines()
                for line in lines:
                    if any(p.search(line) for p in self._patterns):
                        self._queue.put({
                            "filepath": filepath,
                            "line": line.strip(),
                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                        })
                self._positions[filepath] = f.tell()
        except (OSError, IOError) as exc:
            _logger.warning(f"Could not read {filepath}: {exc}")
    
    def on_modified(self, event: FileModifiedEvent) -> None:
        if event.is_directory:
            return
        fp = event.src_path
        if self._should_watch(fp):
            _logger.debug(f"File modified: {fp}")
            self._scan_new_lines(fp)

class Watcher:
    """Main watcher thread that monitors log files for errors."""
    
    def __init__(
        self,
        log_patterns: list[str],
        log_files: list[str],
        log_dirs: list[str],
        poll_interval: float = 1.0
    ) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._observer = Observer()
        self._log_patterns = log_patterns
        self._log_files = log_files
        self._log_dirs = log_dirs
        self._poll_interval = poll_interval
        self._running = False
        _logger.info("Watcher initialized")
    
    def start(self) -> None:
        """Start the watcher thread."""
        _logger.info("Starting log watcher...")
        self._running = True
        self._positions: OrderedDict[str, int] = OrderedDict()
        
        for fp in self._log_files:
            if Path(fp).is_file():
                self._positions[fp] = self._eof_offset(fp)
        
        handler = LogMonitorHandler(
            self._queue, self._log_patterns, self._log_files, self._log_dirs
        )
        self._observer.schedule(handler, path="/", recursive=False)
        self._observer.start()
        _logger.info("Watcher thread running")
    
    def _eof_offset(self, path: str) -> int:
        try:
            return Path(path).stat().st_size
        except OSError:
            return 0
    
    def get_event(self, timeout: float = 1.0):
        """Get error event from queue (non-blocking)."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def stop(self) -> None:
        """Stop the watcher thread."""
        _logger.info("Stopping log watcher...")
        self._running = False
        self._observer.stop()
        self._observer.join()
        _logger.info("Watcher stopped")
