from __future__ import annotations

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
    return connection


def write_samples(connection: sqlite3.Connection, rows: list[tuple[str, float, float]]) -> None:
    if rows:
        connection.executemany("INSERT OR IGNORE INTO samples VALUES (?, ?, ?)", rows)
        connection.commit()


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
    if end <= start:
        return [], []
    width = (end - start) / max_bins
    rows = connection.execute(
        "SELECT CAST((timestamp - ?) / ? AS INTEGER) AS bucket, "
        "MIN(timestamp), MIN(value), MAX(value) FROM samples "
        "WHERE signal = ? AND timestamp BETWEEN ? AND ? GROUP BY bucket ORDER BY bucket",
        (start, width, signal, start, end),
    )
    x: list[float] = []
    y: list[float] = []
    for _, timestamp, low, high in rows:
        x.append(timestamp)
        y.append(low)
        if high != low:
            x.append(timestamp)
            y.append(high)
    return x, y
