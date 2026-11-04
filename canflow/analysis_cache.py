"""Bounded process-wide cache for immutable analysis query results."""
from collections import OrderedDict
from bisect import bisect_left, bisect_right
import math
from threading import Condition, RLock

from PySide6 import QtCore

from .store import connect, curve_viewport_edges, nearest_sample, plot_points_with_samples


class QueryCache:
    def __init__(self, max_bytes: int = 512 * 1024 * 1024):
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        self.max_bytes = max_bytes
        self.size_bytes = 0
        self._entries = OrderedDict()
        self._lock = RLock()

    def get(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry[0]

    def put(self, key, value, size: int):
        if size < 0:
            raise ValueError("size must be non-negative")
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self.size_bytes -= previous[1]
            if size > self.max_bytes:
                return
            self._entries[key] = (value, size)
            self.size_bytes += size
            while self.size_bytes > self.max_bytes:
                _, (_, removed) = self._entries.popitem(last=False)
                self.size_bytes -= removed

    def clear_owner(self, owner):
        with self._lock:
            for key in [key for key in self._entries if key[0] == owner]:
                _, size = self._entries.pop(key)
                self.size_bytes -= size

    def covering_range(self, owner, version, signal, start, end):
        with self._lock:
            for key in reversed(self._entries):
                if (len(key) == 6 and key[:4] == (owner, 'raw', version, signal)
                        and key[4] <= start and key[5] >= end):
                    self._entries.move_to_end(key)
                    return self._entries[key][0], key[4], key[5]
        return None

    def covering_samples(self, owner, version, signal, start, end):
        entry = self.covering_range(owner, version, signal, start, end)
        return entry[0] if entry is not None else None


query_cache = QueryCache()


class AnalysisReader(QtCore.QThread):
    """Read SQLite off the UI thread, coalescing obsolete requests by kind."""
    result = QtCore.Signal(str, int, object)
    failed = QtCore.Signal(str, int, str)

    def __init__(self, path, owner, cache=query_cache):
        super().__init__()
        self.path = path
        self.owner = owner
        self.cache = cache
        self._condition = Condition()
        self._pending = OrderedDict()
        self._stopping = False

    def request(self, kind, generation, args):
        with self._condition:
            self._pending[kind] = (generation, args)
            self._condition.notify()

    def stop(self):
        with self._condition:
            self._stopping = True
            self._pending.clear()
            self._condition.notify()

    def _view(self, connection, signal, start, end, version):
        key = (self.owner, version, signal, start, end)
        data = self.cache.get(key)
        if data is None:
            raw = self.cache.covering_samples(self.owner, version, signal, start, end)
            data = (self._raw_view(raw, start, end) if raw is not None else
                    (*plot_points_with_samples(connection, signal, start, end),
                     curve_viewport_edges(connection, signal, start, end)))
            # Account for Python float/list overhead, not only the 8-byte values.
            size = 512 + 48 * sum(len(part) for part in data[:4]) + 160 * len(data[4])
            self.cache.put(key, data, size)
        return data

    @staticmethod
    def _raw_view(rows, start, end, max_bins=1600):
        if end <= start:
            return [], [], [], [], []
        lo = bisect_left(rows, (start, -math.inf))
        hi = bisect_right(rows, (end, math.inf))
        width = (end - start) / max_bins
        bins = {}
        for timestamp, value in rows[lo:hi]:
            bucket = int((timestamp - start) / width)
            if bucket not in bins:
                bins[bucket] = [timestamp, value, value, value]
            else:
                bins[bucket][2] = min(bins[bucket][2], value)
                bins[bucket][3] = max(bins[bucket][3], value)
        x, y, sx, sy = [], [], [], []
        for timestamp, value, low, high in bins.values():
            x.append(timestamp)
            y.append(low)
            if high != low:
                x.append(timestamp)
                y.append(high)
            sx.append(timestamp)
            sy.append(value)
        edges = []
        for boundary in (start, end):
            after_index = bisect_left(rows, (boundary, -math.inf))
            before_index = bisect_right(rows, (boundary, math.inf)) - 1
            if before_index >= 0 and after_index < len(rows):
                before_time = rows[before_index][0]
                before = rows[bisect_left(rows, (before_time, -math.inf))]
                after = rows[after_index]
                if before[0] < boundary < after[0]:
                    ratio = (boundary - before[0]) / (after[0] - before[0])
                    edges.append((boundary, before[1] + ratio * (after[1] - before[1])))
        return x, y, sx, sy, edges

    def _prefetch_raw(self, connection, signal, start, end, version):
        limit = 50000
        rows = connection.execute(
            'SELECT timestamp, value FROM samples WHERE signal=? AND timestamp BETWEEN ? AND ? '
            'ORDER BY timestamp, value LIMIT ?', (signal, start, end, limit + 1)).fetchall()
        if len(rows) > limit:
            return
        before = connection.execute(
            'SELECT timestamp, value FROM samples WHERE signal=? AND timestamp < ? '
            'ORDER BY timestamp DESC, value ASC LIMIT 1', (signal, start)).fetchone()
        after = connection.execute(
            'SELECT timestamp, value FROM samples WHERE signal=? AND timestamp > ? '
            'ORDER BY timestamp ASC, value ASC LIMIT 1', (signal, end)).fetchone()
        if before is not None:
            rows.insert(0, before)
        if after is not None:
            rows.append(after)
        self.cache.put((self.owner, 'raw', version, signal, start, end), rows, 512 + 128 * len(rows))

    def _cached_nearest(self, signal, timestamp, version):
        entry = self.cache.covering_range(self.owner, version, signal, timestamp, timestamp)
        if entry is None:
            return False, None
        rows, low, high = entry
        before = bisect_right(rows, (timestamp, math.inf)) - 1
        after = bisect_left(rows, (timestamp, -math.inf))
        candidates = ([rows[before]] if before >= 0 else []) + ([rows[after]] if after < len(rows) else [])
        if not candidates:
            return False, None
        sample = min(candidates, key=lambda row: abs(row[0] - timestamp))
        if abs(sample[0] - timestamp) >= min(timestamp - low, high - timestamp):
            return False, None
        return True, sample

    def _prefetch(self, connection, signals, start, end, version):
        width = end - start
        if width <= 0:
            return
        for signal in signals:
            with self._condition:
                if self._stopping or self._pending:
                    return
            self._prefetch_raw(connection, signal, start - width, end + width, version)
        for lo, hi in ((start - width, start), (end, end + width)):
            for signal in signals:
                with self._condition:
                    if self._stopping or self._pending:
                        return
                self._view(connection, signal, lo, hi, version)

    def run(self):
        connection = connect(self.path)
        try:
            while True:
                with self._condition:
                    while not self._pending and not self._stopping:
                        self._condition.wait()
                    if self._stopping:
                        return
                    kind, (generation, args) = self._pending.popitem(last=False)
                try:
                    connection.execute('BEGIN')
                    if kind == 'view':
                        value = plot_points_with_samples(connection, *args)
                    elif kind == 'cursor':
                        value = nearest_sample(connection, *args)
                    elif kind == 'views':
                        signals, start, end, version = args
                        value = {signal: self._view(connection, signal, start, end, version)
                                 for signal in signals}
                    elif kind == 'cursors':
                        signals, timestamp, *version = args
                        value = {}
                        for signal in signals:
                            found, sample = self._cached_nearest(signal, timestamp, version[0]) if version else (False, None)
                            value[signal] = sample if found else nearest_sample(connection, signal, timestamp)
                    else:
                        raise ValueError(f'unknown analysis request: {kind}')
                    self.result.emit(kind, generation, value)
                    if kind == 'views':
                        self._prefetch(connection, signals, start, end, version)
                    connection.commit()
                except Exception as exc:
                    connection.rollback()
                    self.failed.emit(kind, generation, str(exc))
        finally:
            connection.close()
