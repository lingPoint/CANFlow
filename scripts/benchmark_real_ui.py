"""Measure real-data Qt responsiveness and added-signal time to first waveform."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import tempfile
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import can
from PySide6 import QtCore, QtWidgets

from canflow.app import AnalysisWindow
from canflow.core import available_signals, inspect_file, load_databases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blf', type=Path, required=True)
    parser.add_argument('--dbc', type=Path, required=True)
    parser.add_argument('--channel', type=int, required=True)
    args = parser.parse_args()
    info = inspect_file(args.blf)
    counts = Counter()
    with can.BLFReader(args.blf) as reader:
        for number, message in enumerate(reader):
            if number >= 200000:
                break
            if message.channel == args.channel:
                counts[(message.arbitration_id, message.is_extended_id)] += 1
    mapping = {args.channel: args.dbc}
    signals = available_signals(load_databases(mapping))
    signals = [s for s in signals if (s.frame_id, s.extended) in counts]
    signals.sort(key=lambda s: (-counts[(s.frame_id, s.extended)], s.frame_id, s.name))
    first, additional = set(signals[:8]), set(signals[8:16])
    if len(first) != 8 or len(additional) != 8:
        raise ValueError('Need 16 matched signals')
    QtCore.QStandardPaths.setTestModeEnabled(True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    with tempfile.TemporaryDirectory(prefix='canflow-ui-bench-') as folder:
        window = AnalysisWindow()
        window.settings = QtCore.QSettings(str(Path(folder) / 'settings.ini'), QtCore.QSettings.Format.IniFormat)
        gaps = []
        last_tick = [time.perf_counter()]
        timer = QtCore.QTimer()
        def tick():
            now = time.perf_counter()
            gaps.append(now - last_tick[0])
            last_tick[0] = now
        timer.timeout.connect(tick)
        timer.start(10)
        try:
            window._scan_done([info])
            window.mapping = mapping
            window.signals = available_signals(load_databases(mapping))
            window._populate_signals()
            for row in range(window.signal_list.count()):
                item = window.signal_list.item(row)
                if item.data(QtCore.Qt.ItemDataRole.UserRole) in first:
                    item.setCheckState(QtCore.Qt.CheckState.Checked)
            window._start()
            started = time.perf_counter()
            while window.worker is not None and time.perf_counter() - started < 300:
                app.processEvents()
                time.sleep(.001)
            if window.worker is not None:
                raise TimeoutError('replay did not finish')
            replay = time.perf_counter() - started
            midpoint = (info.last - info.first) / 2
            window.follow.setChecked(False)
            window.plot.setXRange(midpoint - 5, midpoint + 5, padding=0)
            for row in range(window.signal_list.count()):
                item = window.signal_list.item(row)
                if item.data(QtCore.Qt.ItemDataRole.UserRole) in additional:
                    item.setCheckState(QtCore.Qt.CheckState.Checked)
            # Ensure the measured time begins when the new selection is applied.
            window.signal_timer.stop()
            gaps.clear()
            last_tick[0] = started = time.perf_counter()
            window._apply_signals()
            for row in range(window.curve_table.rowCount()):
                item = window.curve_table.item(row, 0)
                if item.data(QtCore.Qt.ItemDataRole.UserRole) in additional:
                    item.setCheckState(QtCore.Qt.CheckState.Checked)
            first_visible = None
            while time.perf_counter() - started < 300:
                app.processEvents()
                if first_visible is None and any(window.curve_data.get(key, ([], []))[0] for key in additional):
                    first_visible = time.perf_counter() - started
                if additional <= window.cached and first_visible is not None:
                    break
                time.sleep(.001)
            if not additional <= window.cached or first_visible is None:
                raise TimeoutError('new signals did not finish')
            complete_seconds = time.perf_counter() - started
            while window.worker is not None or getattr(window, 'backfill_worker', None) is not None:
                app.processEvents()
                time.sleep(.001)
            # Let idle prefetch complete before measuring a sequence of nearby pans.
            warm_until = time.perf_counter() + 2
            while time.perf_counter() < warm_until:
                app.processEvents()
                time.sleep(.002)
            pan_times = []
            for shift in (-.8, -.6, -.4, -.2, 0., .2, .4, .6, .8, 0.):
                start = time.perf_counter()
                window.plot.setXRange(midpoint - 5 + shift, midpoint + 5 + shift, padding=0)
                window._refresh_plot(all_visible=True)
                while getattr(window, '_plot_request', None) is not None:
                    app.processEvents()
                    if time.perf_counter() - start > 30:
                        raise TimeoutError('plot did not update')
                    time.sleep(.001)
                pan_times.append(time.perf_counter() - start)
            print(json.dumps(dict(frames=info.frames, replay_seconds=replay,
                                  added_first_visible_seconds=first_visible,
                                  added_complete_seconds=complete_seconds,
                                  pan_mean_seconds=sum(pan_times) / len(pan_times),
                                  pan_max_seconds=max(pan_times),
                                  ui_heartbeat_max_seconds=max(gaps, default=0)), indent=2))
        finally:
            timer.stop()
            window.close()


if __name__ == '__main__':
    main()
