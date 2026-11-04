"""Benchmark replay, signal backfill and indexed queries against a real BLF/DBC.

Example: python scripts/benchmark_real_analysis.py --blf <file> --dbc <file> --channel 4
No recording content is copied or exported; the temporary database is deleted on exit.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import tempfile
import time

import can

from canflow.core import available_signals, inspect_file, load_databases
from canflow.store import connect, nearest_sample, plot_points_with_samples
from canflow.workers import ReplayWorker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blf', type=Path, required=True)
    parser.add_argument('--dbc', type=Path, required=True)
    parser.add_argument('--channel', type=int, required=True)
    parser.add_argument('--sample-frames', type=int, default=200000)
    parser.add_argument('--indexed', action='store_true', help='measure indexed view-first backfill')
    args = parser.parse_args()
    started = time.perf_counter()
    info = inspect_file(args.blf)
    scan_seconds = time.perf_counter() - started
    counts = Counter()
    with can.BLFReader(args.blf) as reader:
        for index, message in enumerate(reader):
            if index >= args.sample_frames:
                break
            if message.channel == args.channel:
                counts[(message.arbitration_id, message.is_extended_id)] += 1
    mapping = {args.channel: args.dbc}
    signals = available_signals(load_databases(mapping))
    signals = [s for s in signals if (s.frame_id, s.extended) in counts]
    signals.sort(key=lambda s: (-counts[(s.frame_id, s.extended)], s.frame_id, s.name))
    if len(signals) < 16:
        raise ValueError('Need at least 16 matched signals in sampled frames')
    groups = [set(signals[:8]), set(signals[8:16])]
    results = dict(bytes=args.blf.stat().st_size, frames=info.frames, channel=args.channel,
                   scan_seconds=scan_seconds, selected_signals=16)
    with tempfile.TemporaryDirectory(prefix='canflow-bench-') as folder:
        path = Path(folder) / 'samples.sqlite'
        db = connect(path)
        indexes = {} if args.indexed else None
        for index, selected in enumerate(groups):
            kwargs = {}
            if indexes is not None:
                mid = (info.first + info.last) / 2
                kwargs = dict(indexes=indexes, priority_range=(mid - 5, mid + 5))
            worker = ReplayWorker([info], mapping, selected, path, backfill=index == 1, **kwargs)
            errors = []
            worker.failed.connect(errors.append)
            started = time.perf_counter()
            if args.indexed and index:
                worker.priority_ready.connect(lambda: results.update(priority_seconds=time.perf_counter() - started))
            worker.run()
            results['backfill_seconds' if index else 'replay_seconds'] = time.perf_counter() - started
            if not worker.succeeded:
                raise RuntimeError(f'replay failed: {errors}')
        results['samples'] = db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
        mid = (info.first + info.last) / 2
        for label, start, end in [('overview', info.first, info.last), ('zoom', mid - 5, mid + 5)]:
            started = time.perf_counter()
            for signal in signals[:16]:
                plot_points_with_samples(db, signal.storage_key(), start, end)
            results[label + '_16_seconds'] = time.perf_counter() - started
        started = time.perf_counter()
        for signal in signals[:16]:
            nearest_sample(db, signal.storage_key(), mid)
        results['cursor_16_seconds'] = time.perf_counter() - started
        db.close()
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
