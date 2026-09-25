"""Reproducible dense 16-channel benchmark (use --size-mib 1024 for 1 GiB).

Every frame is decoded, including CAN FD, classic CAN, channel timestamp rollback,
and short spikes. Results include scan/replay, 16-track overview/zoom and peak RSS.
Run from the repository root: python -m scripts.benchmark_v3 --output .test-data/v3
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import threading
import time
from pathlib import Path

import can

from canflow.core import SignalKey, inspect_file
from canflow.store import connect, plot_points_with_samples
from canflow.workers import ReplayWorker


def generate(directory: Path, size_mib: int) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    blf = directory / f"dense16-{size_mib}m.blf"
    dbc = directory / "dense16.dbc"
    dbc.write_text('VERSION ""\nNS_ :\nBS_: \nBU_: ECU\n'
                   'BO_ 291 Dense: 64 ECU\n'
                   ' SG_ Value : 0|16@1+ (1,0) [0|65535] "" ECU\n', encoding="utf-8")
    if not blf.exists():
        pending = blf.with_suffix(".partial")
        count = 0
        with can.BLFWriter(pending, compression_level=0) as writer:
            while writer.file.tell() < size_mib * (1 << 20):
                for _ in range(10000):
                    cycle, slot = divmod(count, 16)
                    value = 65535 if cycle % 4096 == 0 else (cycle + slot) % 1000
                    payload = value.to_bytes(2, "little") + bytes(range(2, 64))
                    writer.on_message_received(can.Message(
                        timestamp=1_700_000_000 + cycle * 0.00016 + (15 - slot) * 0.000001,
                        channel=slot + 1, arbitration_id=0x123, is_extended_id=False,
                        is_fd=cycle % 10 != 0, data=payload if cycle % 10 != 0 else payload[:8],
                    ))
                    count += 1
        pending.replace(blf)
    return blf, dbc


def rss() -> int:
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "peak", "rss", "paged_peak", "paged", "nonpaged_peak", "nonpaged", "page", "page_peak")]
    data = Counters()
    data.cb = ctypes.sizeof(data)
    get_process = ctypes.windll.kernel32.GetCurrentProcess
    get_process.restype = wintypes.HANDLE
    memory = ctypes.windll.psapi.GetProcessMemoryInfo
    memory.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD)
    if not memory(get_process(), ctypes.byref(data), data.cb):
        raise ctypes.WinError()
    return data.rss


def ui_benchmark(directory, label, info, dbc, keys):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtGui, QtWidgets
    from canflow.app import MainWindow
    from canflow.core import available_signals, load_databases
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope,
                            str(directory.resolve() / "settings"))
    QtCore.QStandardPaths.setTestModeEnabled(True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    font = Path(r"C:\Windows\Fonts\msyh.ttc")
    if font.exists():
        QtGui.QFontDatabase.addApplicationFont(str(font))
        app.setFont(QtGui.QFont("Microsoft YaHei UI", 9))
    window = MainWindow()
    window.resize(1600, 1100)
    gaps = []
    last_tick = [time.perf_counter()]
    timer = QtCore.QTimer()

    def tick():
        now = time.perf_counter()
        gaps.append(now - last_tick[0])
        last_tick[0] = now

    timer.timeout.connect(tick)
    try:
        window._scan_done([info])
        window.mapping = {channel: dbc for channel in range(1, 17)}
        window.signals = available_signals(load_databases(window.mapping))
        window._populate_signals()
        for row in range(window.signal_list.count()):
            window.signal_list.item(row).setCheckState(QtCore.Qt.CheckState.Checked)
        window._apply_signals_without_backfill(mark_dirty=False)
        window.curve_table.blockSignals(True)
        for row in range(window.curve_table.rowCount()):
            window.curve_table.item(row, 0).setCheckState(QtCore.Qt.CheckState.Checked)
        window.curve_table.blockSignals(False)
        for key in keys:
            window.chart.set_visible(key, True)
        window._update_curve_summary()
        window.follow.setChecked(False)
        window.plot.setXRange(0, info.last - info.first, padding=0)
        window.pages.setCurrentIndex(0)
        window.show()
        app.processEvents()
        window._start()
        started = last_tick[0] = time.perf_counter()
        timer.start(20)
        while window.worker is not None and time.perf_counter() - started < 600:
            app.processEvents()
            time.sleep(0.001)
        timer.stop()
        elapsed = time.perf_counter() - started
        assert window.worker is None, "UI replay timed out"
        assert window.progress.value() == 1000
        assert window.raw_table.rowCount() == 1000
        assert {window.raw_table.item(row, 1).text() for row in range(1000)} == {str(i) for i in range(1, 17)}
        assert window.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == info.frames
        window._refresh_plot(all_visible=True)
        assert all(65535 in window.curves[key].yData for key in keys)
        window._set_playback_details_visible(False, persist=False)
        app.processEvents()
        window.grab().save(str(directory / f"{label}-16ch.png"))
        ordered = sorted(gaps)
        assert ordered and max(ordered) < 1.0, f"UI stalled {max(ordered):.3f}s"
        return dict(ui_replay_seconds=elapsed, ui_heartbeat_max_seconds=max(ordered),
                    ui_heartbeat_p99_seconds=ordered[int((len(ordered)-1)*0.99)],
                    ui_visible_tracks=len(keys))
    finally:
        timer.stop()
        window.close()
        app.processEvents()


def benchmark(directory: Path, size_mib: int, label: str, ui: bool = False) -> dict:
    blf, dbc = generate(directory, size_mib)
    cache = directory / f"{label}.sqlite"
    if cache.exists():
        raise ValueError(f"Use a new label; cache already exists: {cache}")
    base = rss()
    peaks = [base]
    done = threading.Event()

    def monitor():
        while not done.wait(0.05):
            peaks[0] = max(peaks[0], rss())

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    try:
        started = time.perf_counter()
        info = inspect_file(blf)
        scan = time.perf_counter() - started
        assert info.channels == tuple(range(1, 17))
        keys = {SignalKey(channel, 0x123, False, "Value") for channel in range(1, 17)}
        worker = ReplayWorker([info], {channel: dbc for channel in range(1, 17)}, keys, cache)
        errors = []
        worker.failed.connect(errors.append)
        started = time.perf_counter()
        worker.run()
        replay = time.perf_counter() - started
        assert worker.succeeded, errors
        connection = connect(cache)
        try:
            count = connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            assert count == info.frames
            counts = dict(connection.execute("SELECT signal, COUNT(*) FROM samples GROUP BY signal"))
            assert counts == {key.storage_key(): info.frames // 16 for key in keys}
            started = time.perf_counter()
            for key in keys:
                x, y, sx, sy = plot_points_with_samples(connection, key.storage_key(), info.first, info.last)
                assert 65535 in y and len(x) <= 3202
                assert len(sx) == len(sy)
            overview = time.perf_counter() - started
            started = time.perf_counter()
            for key in keys:
                plot_points_with_samples(connection, key.storage_key(), info.first + 0.1, info.first + 0.11)
            zoom = time.perf_counter() - started
        finally:
            connection.close()
        ui_result = ui_benchmark(directory, label, info, dbc, keys) if ui else {}
    finally:
        done.set()
        watcher.join()
    result = dict(label=label, python=platform.python_version(), platform=platform.platform(),
                  bytes=blf.stat().st_size, frames=info.frames, channels=16,
                  decoded_samples=count, scan_seconds=scan, replay_seconds=replay,
                  frames_per_second=info.frames/replay, overview_16_seconds=overview,
                  zoom_16_seconds=zoom, peak_rss_mib=max(peaks)/(1 << 20),
                  rss_growth_mib=(max(peaks)-base)/(1 << 20), cache_bytes=cache.stat().st_size, **ui_result)
    (directory / f"{label}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    assert result["rss_growth_mib"] < 512, result
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".test-data/v3"))
    parser.add_argument("--size-mib", type=int, default=64)
    parser.add_argument("--label", default="result")
    parser.add_argument("--ui", action="store_true", help="also replay in the real 16-track Qt window")
    args = parser.parse_args()
    print(json.dumps(benchmark(args.output, args.size_mib, args.label, args.ui), indent=2), flush=True)
