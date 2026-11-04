"""Compare indexed and sequential message streams in three real BLF intervals."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import time

from canflow.blf_index import ContainerIndex, IndexedBLFReader
from canflow.core import inspect_file


def fingerprint(message):
    return (struct.pack('<diI??', message.timestamp,
                        int(message.channel) if message.channel is not None else -1,
                        message.arbitration_id, message.is_extended_id, message.is_fd)
            + bytes([len(message.data)]) + bytes(message.data))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('blf', type=Path)
    args = parser.parse_args()
    info = inspect_file(args.blf)
    span = info.last - info.first
    windows = [(info.first + span * position, info.first + span * position + 5)
               for position in (0.1, 0.5, 0.9)]
    expected = [hashlib.sha256() for _ in windows]
    counts = [0] * len(windows)
    index = ContainerIndex(args.blf)
    started = time.perf_counter()
    with IndexedBLFReader(args.blf, index) as reader:
        for message in reader:
            for number, (start, end) in enumerate(windows):
                if start <= message.timestamp <= end:
                    expected[number].update(fingerprint(message))
                    counts[number] += 1
    scan_seconds = time.perf_counter() - started
    actual = []
    elapsed = []
    for start, end in windows:
        digest = hashlib.sha256()
        started = time.perf_counter()
        count = 0
        for message in index.read_range(start, end):
            digest.update(fingerprint(message))
            count += 1
        elapsed.append(time.perf_counter() - started)
        actual.append((count, digest.digest()))
    assert actual == [(count, digest.digest()) for count, digest in zip(counts, expected)]
    print(json.dumps(dict(bytes=args.blf.stat().st_size, frames=info.frames,
                          indexed_containers=len(index.entries), index_scan_seconds=scan_seconds,
                          interval_counts=counts, interval_seconds=elapsed,
                          matches_sequential=True), indent=2))


if __name__ == '__main__':
    main()
