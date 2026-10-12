"""
core/watcher.py
===============
Log file watcher built on top of the ``watchdog`` library.

Key classes
-----------
ErrorEvent
    Immutable dataclass representing one captured error line.
LogEventHandler
    Watchdog FileSystemEventHandler that tracks byte offsets so it reads
    only newly appended lines, then enqueues those matching an error pattern.
LogWatcher
    High-level manager: parses the paths list, schedules the observer on
    the correct directories, and exposes the error queue.
"""

from __future__ import annotations

import os
import queue
import re
import logging
import config
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from watchdog.events import FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer
from core.watchdog_config import load_rules, rule_pattern


# ─────────────────────────────────────────────────────────────────────────────
# Data class
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ErrorEvent:
    """Represents a single error line detected in a monitored log file."""
    filepath: str
    line: str
    timestamp: str
    triggers: tuple[str, ...] = ()


# ─────────────────────────────────────────────────────────────────────────────
# Watchdog handler
# ─────────────────────────────────────────────────────────────────────────────

class LogEventHandler(FileSystemEventHandler):
    """
    Watchdog event handler.

    On every file-modified event it:
    1. Checks whether the file is in the monitored set.
    2. Reads only the bytes appended since the last check (using a per-file
       byte-offset cache).
    3. Enqueues an :class:`ErrorEvent` for every new line matching any of
       the compiled error patterns.

    Parameters
    ----------
    error_queue
        Thread-safe queue shared with the Orchestrator.
    error_patterns
        List of regex pattern strings (case-insensitive).
    watched_files
        Frozenset of absolute file paths to monitor explicitly.
    watched_dirs
        Frozenset of absolute directory paths; any file inside counts.
    """

    def __init__(
        self,
        error_queue: queue.Queue[ErrorEvent],
        error_patterns: list[str],
        watched_files: frozenset[str],
        watched_dirs: frozenset[str],
        rules_path: str | None = None,
    ) -> None:
        super().__init__()
        self._queue = error_queue
        self._patterns = [re.compile(p, re.IGNORECASE) for p in error_patterns]
        self._pattern_labels = list(error_patterns)
        self._exclude_patterns = []
        self.event_callback = None
        self._rules_path = rules_path
        self._rules_signature = None
        self._rules_error = None
        self._rules_loaded_at = None
        self._reload_rules()
        self._watched_files = watched_files
        self._watched_dirs = watched_dirs
        # filepath → last read byte offset
        self._positions: dict[str, int] = {}

        # Seek all explicitly watched files to EOF so we only capture
        # lines appended *after* the monitor starts.
        for fp in watched_files:
            if Path(fp).is_file():
                self._positions[fp] = self._eof_offset(fp)

    # ── Watchdog callback ─────────────────────────────────────────────

    def on_modified(self, event: FileModifiedEvent) -> None:
        if event.is_directory:
            return
        fp: str = event.src_path
        if self._should_watch(fp):
            self._scan_new_lines(fp)

    # ── Internal helpers ──────────────────────────────────────────────

    def _should_watch(self, filepath: str) -> bool:
        """Return True if *filepath* belongs to the monitored set."""
        try:
            Path(filepath).resolve().relative_to(Path(config.RUNTIME_PATH).resolve())
            return False  # Never detect the monitor's own heartbeat and event trace.
        except ValueError:
            pass
        if filepath in self._watched_files:
            return True
        fp = Path(filepath)
        for d in self._watched_dirs:
            try:
                fp.relative_to(d)
                return True
            except ValueError:
                pass
        return False

    def _scan_new_lines(self, filepath: str) -> None:
        """Read newly appended content and enqueue matching lines."""
        self._reload_rules()
        # First time we see this file from a watched directory: seek to EOF
        if filepath not in self._positions:
            self._positions[filepath] = self._eof_offset(filepath)
            return  # Don't process pre-existing content

        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._positions[filepath])
                for raw_line in fh:
                    stripped = raw_line.rstrip("\r\n")
                    matched = tuple(label for label, pattern in zip(self._pattern_labels, self._patterns) if pattern.search(stripped)) if stripped else ()
                    ignored = tuple(label for label, pattern in self._exclude_patterns if pattern.search(stripped)) if matched else ()
                    if matched:
                        timestamp = datetime.now().astimezone().isoformat()
                        if self.event_callback:
                            self.event_callback({'error': stripped, 'source': filepath, 'detected': timestamp,
                                                 'triggers': list(matched), 'ignore_rules': list(ignored)}, ignored=bool(ignored))
                    if matched and not ignored:
                        self._queue.put(ErrorEvent(
                                filepath=filepath,
                                line=stripped,
                                timestamp=timestamp, triggers=matched,
                            ))
                self._positions[filepath] = fh.tell()
        except OSError:
            pass

    def _reload_rules(self) -> None:
        if not self._rules_path:
            return
        try:
            path = Path(self._rules_path)
            info = path.stat() if path.exists() else None
            signature = (info.st_mtime_ns, info.st_size) if info else ('defaults',)
            if signature == self._rules_signature:
                return
            rules = load_rules(path)
            detect = [rule for rule in rules if rule['enabled'] and rule.get('action', 'Detect') == 'Detect']
            patterns = [re.compile(rule_pattern(rule), re.IGNORECASE) for rule in detect]
            self._patterns = patterns
            self._pattern_labels = [rule['pattern'] for rule in detect]
            self._exclude_patterns = [(rule['pattern'], re.compile(rule_pattern(rule), re.IGNORECASE))
                                      for rule in rules if rule['enabled'] and rule.get('action') == 'Ignore']
            self._rules_signature = signature
            self._rules_error = None
            self._rules_loaded_at = datetime.now().astimezone().isoformat()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._rules_error = str(exc)
            logging.getLogger(__name__).warning('Could not reload watchdog rules; keeping previous rules: %s', exc)

    def rules_status(self):
        return {'detection_count': len(self._patterns), 'ignore_count': len(self._exclude_patterns),
                'loaded_at': self._rules_loaded_at, 'error': self._rules_error}

    @staticmethod
    def _eof_offset(filepath: str) -> int:
        """Return the current end-of-file byte offset."""
        try:
            with open(filepath, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                return fh.tell()
        except OSError:
            return 0


# ─────────────────────────────────────────────────────────────────────────────
# High-level manager
# ─────────────────────────────────────────────────────────────────────────────

class LogWatcher:
    """
    Parses the list of paths from ``logs.lst``, schedules the watchdog
    Observer appropriately, and exposes an ``error_queue``.

    File paths  → observer scheduled on parent dir (non-recursive).
    Dir paths   → observer scheduled on the dir itself (recursive).
    """

    def __init__(self, paths: list[str], error_patterns: list[str], rules_path: str | None = None) -> None:
        self._error_queue: queue.Queue[ErrorEvent] = queue.Queue()

        watched_files: set[str] = set()
        watched_dirs: set[str] = set()

        # Classify each configured path
        for raw in paths:
            p = Path(raw)
            if p.is_file():
                watched_files.add(str(p.resolve()))
            elif p.is_dir():
                watched_dirs.add(str(p.resolve()))
            # Non-existent paths are silently ignored; they may appear later.

        self._handler = LogEventHandler(
            error_queue=self._error_queue,
            error_patterns=error_patterns,
            watched_files=frozenset(watched_files),
            watched_dirs=frozenset(watched_dirs),
            rules_path=rules_path,
        )

        self._observer = Observer()
        scheduled: set[str] = set()

        for raw in paths:
            p = Path(raw)
            if p.is_file():
                parent = str(p.parent.resolve())
                if parent not in scheduled:
                    self._observer.schedule(self._handler, parent, recursive=False)
                    scheduled.add(parent)
            elif p.is_dir():
                d = str(p.resolve())
                if d not in scheduled:
                    self._observer.schedule(self._handler, d, recursive=True)
                    scheduled.add(d)

    # ── Public API ────────────────────────────────────────────────────

    @property
    def error_queue(self) -> queue.Queue[ErrorEvent]:
        """Thread-safe queue of :class:`ErrorEvent` objects."""
        return self._error_queue

    def start(self) -> None:
        """Start the background watchdog thread."""
        self._observer.start()

    def stop(self) -> None:
        """Stop and join the watchdog thread."""
        self._observer.stop()
        self._observer.join()
