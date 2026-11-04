"""Temporary BLF container time index for prioritizing a visible interval.

Uses python-can's container parser, including its saved cross-container tail.
The index is only a priority hint: normal sequential backfill remains authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
import zlib

import can
from can.io.blf import (FILE_HEADER_STRUCT, LOG_CONTAINER, LOG_CONTAINER_STRUCT,
                        NO_COMPRESSION, OBJ_HEADER_BASE_STRUCT, ZLIB_DEFLATE)


def _signature(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino


@dataclass(frozen=True)
class ContainerEntry:
    start: int
    end: int
    low: float
    high: float
    tail: bytes


class ContainerIndex:
    def __init__(self, path: Path, max_bytes: int = 16 * 1024 * 1024):
        self.path = Path(path)
        self.signature = _signature(self.path)
        self.max_bytes = max_bytes
        self._bytes = 0
        self._lock = Lock()
        self.entries: list[ContainerEntry] = []
        self._ends = self._container_ends()

    def _container_ends(self):
        result = {}
        with self.path.open('rb') as stream:
            header = FILE_HEADER_STRUCT.unpack(stream.read(FILE_HEADER_STRUCT.size))
            stream.seek(header[1])
            while True:
                start = stream.tell()
                data = stream.read(OBJ_HEADER_BASE_STRUCT.size)
                if not data:
                    break
                if len(data) != OBJ_HEADER_BASE_STRUCT.size:
                    break
                signature, _, _, size, kind = OBJ_HEADER_BASE_STRUCT.unpack(data)
                if signature != b'LOBJ' or size < OBJ_HEADER_BASE_STRUCT.size:
                    break
                end = start + size + size % 4
                if kind == LOG_CONTAINER:
                    result[end] = start
                stream.seek(end)
        return result

    def add(self, end, low, high, tail):
        start = self._ends.get(end)
        if start is None or low is None:
            return
        entry = ContainerEntry(start, end, low, high, bytes(tail))
        size = 192 + len(entry.tail)
        with self._lock:
            if self._bytes + size <= self.max_bytes:
                self.entries.append(entry)
                self._bytes += size

    def read_range(self, start, end):
        if _signature(self.path) != self.signature:
            return
        with self._lock:
            entries = [entry for entry in self.entries if entry.low <= end and entry.high >= start]
        with can.BLFReader(self.path) as reader:
            for entry in entries:
                reader.file.seek(entry.start)
                data = reader.file.read(OBJ_HEADER_BASE_STRUCT.size)
                _, _, _, size, kind = OBJ_HEADER_BASE_STRUCT.unpack(data)
                obj_data = reader.file.read(size - OBJ_HEADER_BASE_STRUCT.size)
                if kind != LOG_CONTAINER:
                    continue
                method, _ = LOG_CONTAINER_STRUCT.unpack_from(obj_data)
                payload = obj_data[LOG_CONTAINER_STRUCT.size:]
                if method == ZLIB_DEFLATE:
                    payload = zlib.decompress(payload)
                elif method != NO_COMPRESSION:
                    continue
                reader._tail = entry.tail
                for message in reader._parse_container(payload):
                    if start <= message.timestamp <= end:
                        yield message


class IndexedBLFReader(can.BLFReader):
    def __init__(self, path, index: ContainerIndex):
        super().__init__(path)
        self.index = index

    def _parse_container(self, data):
        end = self.file.tell()
        tail = self._tail
        low = high = None
        for message in super()._parse_container(data):
            timestamp = float(message.timestamp)
            low = timestamp if low is None else min(low, timestamp)
            high = timestamp if high is None else max(high, timestamp)
            yield message
        self.index.add(end, low, high, tail)
