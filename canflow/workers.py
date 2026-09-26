from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

import can
from PySide6 import QtCore

from .core import (FileInfo, SignalKey, available_signals, prepare_decoders, inspect_file,
                   load_databases, signal_definition_fingerprint, usable_frame, validate_sequence)
from .persistence import SignalReference
from .store import configure_overview, connect, write_samples


class ScanCache:
    """Bounded session cache; file replacement/modification invalidates metadata."""

    def __init__(self, capacity: int = 32):
        self.capacity = capacity
        self._entries = OrderedDict()

    def inspect(self, path: Path, cancelled, progress=None) -> FileInfo:
        if cancelled():
            raise InterruptedError("预检已取消")
        path = path.resolve()
        before = path.stat()
        signature = (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino)
        cached = self._entries.get(path)
        if cached and cached[0] == signature:
            self._entries.move_to_end(path)
            return cached[1]
        info = inspect_file(path, cancelled, progress) if progress else inspect_file(path, cancelled)
        after = path.stat()
        if signature != (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino):
            raise ValueError(f"{path.name}: 预检期间文件发生变化，请重新导入")
        self._entries[path] = (signature, info)
        self._entries.move_to_end(path)
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)
        return info


class ScanWorker(QtCore.QThread):
    scanned = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    progress = QtCore.Signal(str, int, int, int)

    def __init__(self, paths: list[Path], cache: ScanCache | None = None):
        super().__init__()
        self.paths = paths
        self.cache = cache if cache is not None else ScanCache()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            paths = list(dict.fromkeys(path.resolve() for path in self.paths))
            files = []
            last_emit = 0.0
            for index, path in enumerate(paths):
                size = path.stat().st_size
                def report(position):
                    nonlocal last_emit
                    now = time.monotonic()
                    if now - last_emit >= 0.1 or position >= size:
                        last_emit = now
                        self.progress.emit(path.name, index + 1, len(paths), min(100, position * 100 // max(1, size)))
                self.progress.emit(path.name, index + 1, len(paths), 0)
                files.append(self.cache.inspect(path, self._stop.is_set, report))
                report(size)
            files = validate_sequence(files)
            if self._stop.is_set():
                return
            self.scanned.emit(files)
        except InterruptedError:
            pass
        except Exception as exc:
            self.failed.emit(str(exc))


class DbcLoadWorker(QtCore.QThread):
    """Prepare an atomic mapping/selection update off the GUI thread."""

    def __init__(self, mapping, channel, path, selected, unresolved):
        super().__init__()
        self.mapping = dict(mapping)
        self.channel = channel
        self.path = path
        self.selected = set(selected)
        self.unresolved = dict(unresolved)
        self.result = None
        self.error = None

    def run(self):
        try:
            try:
                old = load_databases(self.mapping)
            except Exception:
                old = {}
            references = dict(self.unresolved)
            for key in self.selected:
                references[key] = SignalReference(key, signal_definition_fingerprint(key, old))
            if self.isInterruptionRequested():
                return
            mapping = {**self.mapping, self.channel: self.path.resolve()}
            databases = load_databases(mapping)
            signals = available_signals(databases)
            available = set(signals)
            selected, unresolved = set(), {}
            for key, reference in references.items():
                current = signal_definition_fingerprint(key, databases)
                if key in available and (not reference.fingerprint or current == reference.fingerprint):
                    selected.add(key)
                else:
                    unresolved[key] = reference
            if not self.isInterruptionRequested():
                self.result = (mapping, signals, selected, unresolved)
        except Exception as exc:
            self.error = str(exc)


class ReplayWorker(QtCore.QThread):
    advanced = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    completed = QtCore.Signal(bool)

    def __init__(
        self,
        files: list[FileInfo],
        mapping: dict[int, Path],
        selected: set[SignalKey],
        cache_path: Path,
        start_at: float | None = None,
        backfill: bool = False,
        poll_updates: bool = False,
    ):
        super().__init__()
        self.files = files
        self.mapping = mapping
        self.selected = selected
        self.cache_path = cache_path
        self.start_at = start_at
        self.backfill = backfill
        self.poll_updates = poll_updates
        self._update_lock = threading.Lock()
        self._latest_update = None
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self.succeeded = False

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()

    def set_paused(self, paused: bool) -> None:
        (self._resume.clear if paused else self._resume.set)()

    def take_update(self):
        """Consume a single latest snapshot; a slow UI cannot queue old frames."""
        with self._update_lock:
            result, self._latest_update = self._latest_update, None
        return result

    def _publish(self, file_index, processed, total, timestamp, messages):
        rows = [(float(msg.timestamp), int(msg.channel) if msg.channel is not None else -1,
                 msg.arbitration_id, "FD" if msg.is_fd else "CAN", msg.data.hex(" ").upper())
                for msg in messages]
        update = (file_index, processed, total, timestamp, rows)
        if self.poll_updates:
            with self._update_lock:
                self._latest_update = update
        else:
            self.advanced.emit(update)

    def run(self) -> None:
        connection = None
        try:
            databases = load_databases(self.mapping)
            connection = connect(self.cache_path)
            configure_overview(connection, self.files[0].first, self.files[-1].last)
            decoders = prepare_decoders(databases, self.selected)
            processed = 0
            total = sum(item.frames for item in self.files)
            batch: list[tuple[str, float, float]] = []
            raw_rows: deque[can.Message] = deque(maxlen=1000)
            last_emit = time.monotonic()
            last_time = self.files[0].first
            for file_index, info in enumerate(self.files):
                if self._stop.is_set():
                    break
                if self.start_at is not None and info.last < self.start_at:
                    processed += info.frames
                    continue
                with can.BLFReader(info.path) as reader:
                    for message in reader:
                        if self._stop.is_set():
                            break
                        if not self._resume.is_set():
                            self._resume.wait()
                        if self._stop.is_set() or not usable_frame(message):
                            continue
                        processed += 1
                        timestamp = float(message.timestamp)
                        last_time = max(last_time, timestamp)
                        if self.start_at is not None and timestamp < self.start_at:
                            continue
                        channel = int(message.channel) if message.channel is not None else -1
                        decoder = decoders.get((channel, message.arbitration_id, message.is_extended_id))
                        if decoder:
                            batch.extend((storage, timestamp, value) for storage, value in decoder(message.data))
                        if not self.backfill:
                            raw_rows.append(message)
                        if len(batch) >= 16000:
                            write_samples(connection, batch)
                            batch.clear()
                        if processed % 256:
                            continue
                        now = time.monotonic()
                        if now - last_emit >= 0.25:
                            write_samples(connection, batch)
                            batch.clear()
                            self._publish(file_index, processed, total, last_time, raw_rows)
                            last_emit = now
                write_samples(connection, batch)
                batch.clear()
                self._publish(file_index, processed, total, last_time, raw_rows)
            self.succeeded = not self._stop.is_set()
            self.completed.emit(self.succeeded)
        except Exception as exc:
            self.failed.emit(str(exc))
            self.completed.emit(False)
        finally:
            if connection is not None:
                connection.close()
