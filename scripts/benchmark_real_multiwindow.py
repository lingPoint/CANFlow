"""Exercise two independent real-data windows and report peak process RSS."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import can
from PySide6 import QtCore, QtWidgets
from canflow.app import MainWindow
from canflow.core import available_signals, inspect_file, load_databases
from scripts.benchmark_v3 import rss


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blf', type=Path, required=True)
    parser.add_argument('--dbc', type=Path, required=True)
    parser.add_argument('--channel', type=int, required=True)
    args = parser.parse_args()
    info = inspect_file(args.blf)
    mapping = {args.channel: args.dbc}
    counts = Counter()
    with can.BLFReader(args.blf) as reader:
        for number, message in enumerate(reader):
            if number >= 200000:
                break
            if message.channel == args.channel:
                counts[(message.arbitration_id, message.is_extended_id)] += 1
    signals = available_signals(load_databases(mapping))
    signals = [s for s in signals if (s.frame_id, s.extended) in counts]
    signals.sort(key=lambda s: (-counts[(s.frame_id, s.extended)], s.frame_id, s.name))
    chosen = set(signals[:4])
    if len(chosen) != 4:
        raise ValueError('Need four matched signals')
    QtCore.QStandardPaths.setTestModeEnabled(True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    window.show()
    try:
        for session in window.sessions:
            session._scan_done([info])
            session.mapping = mapping.copy()
            session.signals = available_signals(load_databases(mapping))
            session._populate_signals()
            for row in range(session.signal_list.count()):
                item = session.signal_list.item(row)
                if item.data(QtCore.Qt.ItemDataRole.UserRole) in chosen:
                    item.setCheckState(QtCore.Qt.CheckState.Checked)
            session._start()
        started = time.perf_counter()
        peak = rss()
        ticks = []
        last = started
        while any(s.worker is not None for s in window.sessions):
            app.processEvents()
            now = time.perf_counter()
            ticks.append(now - last)
            last = now
            peak = max(peak, rss())
            if now - started > 300:
                raise TimeoutError('multiwindow replay did not finish')
            time.sleep(.01)
        counts = [s.db.execute('SELECT COUNT(*) FROM samples').fetchone()[0] for s in window.sessions]
        if min(counts) == 0:
            raise RuntimeError('a window produced no samples')
        print(json.dumps(dict(frames_per_window=info.frames, windows=len(window.sessions),
                              seconds=time.perf_counter() - started, sample_counts=counts,
                              peak_rss_mib=peak / 1048576,
                              max_event_gap_seconds=max(ticks, default=0)), indent=2))
    finally:
        for session in window.sessions:
            session.project_dirty = False
        window.close()
        app.processEvents()


if __name__ == '__main__':
    main()
