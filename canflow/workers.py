from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path

import can
from PySide6 import QtCore

from .core import FileInfo, SignalKey, decode_selected, inspect_file, load_databases, usable_frame
from .store import connect, write_samples


class ScanWorker(QtCore.QThread):
    scanned = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, paths: list[Path]):
        super().__init__()
        self.paths = paths
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            files = [inspect_file(path, self._stop.is_set) for path in self.paths]
            files.sort(key=lambda item: (item.first, str(item.path).lower()))
            for previous, current in zip(files, files[1:]):
                if current.first < previous.last:
                    raise ValueError(
                        f"时间重叠：{previous.path.name} 结束于 {previous.last:.6f}，"
                        f"{current.path.name} 开始于 {current.first:.6f}"
                    )
            self.scanned.emit(files)
        except InterruptedError:
            pass
        except Exception as exc:
            self.failed.emit(str(exc))


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
    ):
        super().__init__()
        self.files = files
        self.mapping = mapping
        self.selected = selected
        self.cache_path = cache_path
        self.start_at = start_at
        self.backfill = backfill
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()

    def set_paused(self, paused: bool) -> None:
        (self._resume.clear if paused else self._resume.set)()

    def run(self) -> None:
        connection = None
        try:
            databases = load_databases(self.mapping)
            connection = connect(self.cache_path)
            # Index once: no per-frame search through all selected signals.
            wanted: dict[tuple[int, int, bool], set[SignalKey]] = {}
            for key in self.selected:
                wanted.setdefault((key.channel, key.frame_id, key.extended), set()).add(key)
            processed = 0
            total = sum(item.frames for item in self.files)
            batch: list[tuple[str, float, float]] = []
            raw_rows: deque[tuple[float, int, int, str, str]] = deque(maxlen=1000)
            last_emit = time.monotonic()
            last_time = self.files[0].first
            for file_index, info in enumerate(self.files):
                if self._stop.is_set():
                    break
                with can.BLFReader(info.path) as reader:
                    for message in reader:
                        if self._stop.is_set():
                            break
                        self._resume.wait()
                        if self._stop.is_set() or not usable_frame(message):
                            continue
                        processed += 1
                        timestamp = float(message.timestamp)
                        last_time = timestamp
                        if self.start_at is not None and timestamp < self.start_at:
                            continue
                        channel = int(message.channel) if message.channel is not None else -1
                        keys = wanted.get((channel, message.arbitration_id, message.is_extended_id), set())
                        if keys:
                            decoded = decode_selected(message, databases.get(channel), keys)
                            batch.extend((key.storage_key(), timestamp, value) for key, value in decoded)
                        if not self.backfill:
                            raw_rows.append((timestamp, channel, message.arbitration_id,
                                             "FD" if message.is_fd else "CAN", message.data.hex(" ").upper()))
                        now = time.monotonic()
                        if len(batch) >= 2000 or now - last_emit >= 0.25:
                            write_samples(connection, batch)
                            batch.clear()
                            self.advanced.emit((file_index, processed, total, last_time, list(raw_rows)))
                            raw_rows.clear()
                            last_emit = now
                write_samples(connection, batch)
                batch.clear()
                self.advanced.emit((file_index, processed, total, last_time, list(raw_rows)))
                raw_rows.clear()
            self.completed.emit(not self._stop.is_set())
        except Exception as exc:
            self.failed.emit(str(exc))
            self.completed.emit(False)
        finally:
            if connection is not None:
                connection.close()
