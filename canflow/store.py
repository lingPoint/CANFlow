from __future__ import annotations

import math
import sqlite3
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS samples (signal TEXT NOT NULL, timestamp REAL NOT NULL, value REAL NOT NULL, "
        "PRIMARY KEY (signal, timestamp, value)) WITHOUT ROWID"
    )
    connection.execute("CREATE TABLE IF NOT EXISTS overview_config (origin REAL, step REAL)")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS overview (signal TEXT, level INTEGER, bucket INTEGER, "
        "first_ts REAL, first_value REAL, low REAL, high REAL, "
        "PRIMARY KEY(signal, level, bucket)) WITHOUT ROWID"
    )
    return connection


def configure_overview(connection: sqlite3.Connection, first: float, last: float) -> None:
    """Configure two disk-backed resolutions for this recording's capture range.

    Raw samples remain authoritative for cursors and close zoom. The fine level
    has 25,600 time buckets; the coarse level 1,600. Configuration survives seek
    and backfill, and summaries merge correctly even for timestamp rollbacks.
    """
    if connection.execute("SELECT 1 FROM overview_config").fetchone():
        return
    step = max((last - first) / 25600, 1e-6)
    connection.execute("INSERT INTO overview_config VALUES (?, ?)", (first, step))
    cursor = connection.execute("SELECT signal, timestamp, value FROM samples")
    while rows := cursor.fetchmany(16000):
        _write_overview(connection, rows, first, step)
    connection.commit()


def _merge(target: dict, key, timestamp: float, value: float, low: float, high: float) -> None:
    item = target.get(key)
    if item is None:
        target[key] = [timestamp, value, low, high]
    else:
        if (timestamp, value) < (item[0], item[1]):
            item[0], item[1] = timestamp, value
        if low < item[2]:
            item[2] = low
        if high > item[3]:
            item[3] = high


def _write_overview(connection, rows, origin, step):
    fine = {}
    for signal, timestamp, value in rows:
        bucket = math.floor((timestamp - origin) / step)
        _merge(fine, (signal, bucket), timestamp, value, value, value)
    coarse = {}
    for (signal, bucket), (timestamp, value, low, high) in fine.items():
        _merge(coarse, (signal, bucket // 16), timestamp, value, low, high)
    connection.executemany(
        "INSERT INTO overview VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(signal, level, bucket) DO UPDATE SET "
        "first_ts = MIN(first_ts, excluded.first_ts), "
        "first_value = CASE WHEN excluded.first_ts < first_ts THEN excluded.first_value "
        "WHEN excluded.first_ts = first_ts THEN MIN(first_value, excluded.first_value) ELSE first_value END, "
        "low = MIN(low, excluded.low), high = MAX(high, excluded.high)",
        ((signal, level, bucket, *values) for level, summary in enumerate((fine, coarse))
         for (signal, bucket), values in summary.items()),
    )


def write_samples(connection: sqlite3.Connection, rows: list[tuple[str, float, float]]) -> None:
    if rows:
        with connection:
            # Group B-tree writes by signal instead of jumping between 16+ keys.
            connection.executemany("INSERT OR IGNORE INTO samples VALUES (?, ?, ?)", sorted(rows))
            config = connection.execute("SELECT origin, step FROM overview_config").fetchone()
            if config:
                _write_overview(connection, rows, *config)


def nearest_sample(
    connection: sqlite3.Connection, signal: str, timestamp: float
) -> tuple[float, float] | None:
    """Find the nearest decoded value without loading a signal's history."""
    before = connection.execute(
        "SELECT timestamp, value FROM samples WHERE signal = ? AND timestamp <= ? "
        "ORDER BY timestamp DESC LIMIT 1", (signal, timestamp)
    ).fetchone()
    after = connection.execute(
        "SELECT timestamp, value FROM samples WHERE signal = ? AND timestamp >= ? "
        "ORDER BY timestamp ASC LIMIT 1", (signal, timestamp)
    ).fetchone()
    candidates = [row for row in (before, after) if row is not None]
    return min(candidates, key=lambda row: abs(row[0] - timestamp)) if candidates else None


def plot_points(
    connection: sqlite3.Connection, signal: str, start: float, end: float, max_bins: int = 1600
) -> tuple[list[float], list[float]]:
    x, y, _, _ = plot_points_with_samples(connection, signal, start, end, max_bins)
    return x, y


def plot_points_with_samples(
    connection: sqlite3.Connection, signal: str, start: float, end: float, max_bins: int = 1600
) -> tuple[list[float], list[float], list[float], list[float]]:
    """Return the overview curve plus actual frame samples for bold markers."""
    if max_bins <= 0:
        raise ValueError("max_bins must be positive")
    if end <= start:
        return [], [], [], []
    width = (end - start) / max_bins
    config = connection.execute("SELECT origin, step FROM overview_config").fetchone()
    if config and width >= config[1]:
        return _overview_points(connection, signal, start, end, max_bins, *config)
    rows = connection.execute(
        "WITH bins AS ("
        "SELECT CAST((timestamp - ?) / ? AS INTEGER) AS bucket, "
        "MIN(timestamp) AS first_ts, MIN(value) AS low, MAX(value) AS high "
        "FROM samples WHERE signal = ? AND timestamp BETWEEN ? AND ? GROUP BY bucket"
        ") SELECT first_ts, low, high, "
        "(SELECT value FROM samples WHERE signal = ? AND timestamp = first_ts "
        "ORDER BY value LIMIT 1) AS sample_value "
        "FROM bins ORDER BY bucket",
        (start, width, signal, start, end, signal),
    )
    x: list[float] = []
    y: list[float] = []
    sample_x: list[float] = []
    sample_y: list[float] = []
    for timestamp, low, high, sample_value in rows:
        x.append(timestamp)
        y.append(low)
        if high != low:
            x.append(timestamp)
            y.append(high)
        sample_x.append(timestamp)
        sample_y.append(sample_value)
    return x, y, sample_x, sample_y


def _overview_points(connection, signal, start, end, max_bins, origin, step):
    width = (end - start) / max_bins
    level = 1 if step * 16 <= width else 0
    step *= 16 ** level
    # Only use tiles wholly within the viewport. Boundary tiles use exact raw
    # samples, so an off-screen spike never leaks into the visible Y range.
    first_bucket = math.ceil((start - origin) / step)
    stop_bucket = math.floor((end - origin) / step)
    # Float epoch arithmetic can round a tile edge. Leave one guard tile on
    # either side in the raw path, using identical boundaries for both queries.
    first_bucket += 1
    stop_bucket -= 1
    left = origin + first_bucket * step
    right = origin + stop_bucket * step
    if right <= left:
        # Narrow views generally take the original raw SQL path; this handles
        # unusually small max_bins without recursive resolution selection.
        first_bucket = stop_bucket
        left = right = end
    bins = {}
    for timestamp, value, low, high in connection.execute(
        "SELECT first_ts, first_value, low, high FROM overview "
        "WHERE signal = ? AND level = ? AND bucket >= ? AND bucket < ? ORDER BY bucket",
        (signal, level, first_bucket, stop_bucket),
    ):
        bucket = min(max_bins, int((timestamp - start) / width))
        _merge(bins, bucket, timestamp, value, low, high)
    # Overlap one floating-point ULP at tile boundaries. Duplicate extrema are
    # harmless; missing a sample rounded onto the neighboring tile is not.
    guard = max(math.ulp(start), math.ulp(end), math.ulp(origin)) * 2
    for lo, hi in ((start, min(left + guard, end)), (max(right - guard, start), end)):
        for timestamp, value in connection.execute(
            "SELECT timestamp, value FROM samples WHERE signal = ? AND timestamp BETWEEN ? AND ?",
            (signal, lo, hi),
        ):
            bucket = min(max_bins, int((timestamp - start) / width))
            _merge(bins, bucket, timestamp, value, value, value)
    x, y, sx, sy = [], [], [], []
    for _, (timestamp, value, low, high) in sorted(bins.items()):
        x.append(timestamp)
        y.append(low)
        if high != low:
            x.append(timestamp)
            y.append(high)
        sx.append(timestamp)
        sy.append(value)
    return x, y, sx, sy
