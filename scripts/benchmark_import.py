"""Measure BLF import and DBC assignment, including Qt event-loop stalls.

python -m scripts.benchmark_import --label before --copies 3
Use --skip-blf for the DBC-only loop; results go to .test-data/import/.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui, QtWidgets
from can.io.blf import FILE_HEADER_STRUCT, timestamp_to_systemtime

from canflow.app import MainWindow
from canflow.workers import ScanCache


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--copies", type=int, default=3)
    parser.add_argument("--skip-blf", action="store_true")
    parser.add_argument("--ui-blf", action="store_true")
    args = parser.parse_args()
    root = Path(".test-data/import")
    root.mkdir(parents=True, exist_ok=True)
    results = {}
    if not args.skip_blf:
        source = Path(".test-data/v3/dense16-1024m.blf")
        paths = [source]
        for index in range(1, args.copies):
            dest = root / f"dense16-{index}.blf"
            if not dest.exists():
                shutil.copyfile(source, dest)
                with dest.open("r+b") as stream:
                    header = list(FILE_HEADER_STRUCT.unpack(stream.read(FILE_HEADER_STRUCT.size)))
                    header[14:22] = timestamp_to_systemtime(1700000000 + index * 1000)
                    header[22:30] = timestamp_to_systemtime(1700000100 + index * 1000)
                    stream.seek(0)
                    stream.write(FILE_HEADER_STRUCT.pack(*header))
            paths.append(dest)
        cache = ScanCache()
        started = time.perf_counter()
        infos = [cache.inspect(path, lambda: False) for path in paths]
        results["blf_seconds"] = time.perf_counter() - started
        results["blf_bytes"] = sum(path.stat().st_size for path in paths)
        results["blf_metadata"] = [dict(first=i.first, last=i.last, frames=i.frames,
                                        channels=i.channels, max_payloads=i.max_payloads) for i in infos]
        started = time.perf_counter()
        for path in paths:
            cache.inspect(path, lambda: False)
        results["blf_cached_seconds"] = time.perf_counter() - started
        print(json.dumps(results), flush=True)
    dbc = root / "large.dbc"
    dbc.write_text('VERSION ""\nNS_ :\nBS_: \nBU_: ECU\n' + "".join(
        f'BO_ {i + 1} Message{i}: 64 ECU\n' + "".join(
            f' SG_ Value{j} : {j * 16}|16@1+ (0.1,0) [0|6553.5] "V" ECU\n'
            for j in range(32)) for i in range(1000)), encoding="utf-8")
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope,
                            str(root.resolve() / "settings"))
    QtCore.QStandardPaths.setTestModeEnabled(True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    font = Path(r"C:\Windows\Fonts\msyh.ttc")
    if font.exists():
        QtGui.QFontDatabase.addApplicationFont(str(font))
        app.setFont(QtGui.QFont("Microsoft YaHei UI", 9))
    window = MainWindow()
    window.pages.setCurrentIndex(1)
    window.show()
    app.processEvents()
    gaps = []
    last = [time.perf_counter()]
    timer = QtCore.QTimer()
    def tick():
        now = time.perf_counter()
        gaps.append(now - last[0])
        last[0] = now
    timer.timeout.connect(tick)
    timer.start(10)
    results["dbc_assignments"] = []
    try:
        if args.ui_blf and not args.skip_blf:
            from unittest.mock import patch
            gaps.clear()
            started = last[0] = time.perf_counter()
            with patch.object(QtWidgets.QFileDialog, "getOpenFileNames", return_value=(list(map(str, paths)), "")):
                window._add_files()
            while window.scanner is not None:
                app.processEvents()
                time.sleep(0.001)
                assert time.perf_counter() - started < 240
            tick()
            assert window.files == infos
            results["blf_ui_seconds"] = time.perf_counter() - started
            results["blf_ui_max_gap_seconds"] = max(gaps)
        for channel in (1, 2):
            started = last[0] = time.perf_counter()
            gaps.clear()
            window._assign_dbc_path(dbc, channel)
            returned = time.perf_counter() - started
            while getattr(window, "dbc_loader", None) is not None:
                app.processEvents()
                time.sleep(0.001)
                assert time.perf_counter() - started < 120
            app.processEvents()
            tick()
            results["dbc_assignments"].append(dict(channel=channel,
                return_seconds=returned, total_seconds=time.perf_counter() - started,
                max_ui_gap_seconds=max(gaps), signals=window.signal_list.count()))
            assert window.mapping[channel] == dbc.resolve()
            assert window.signal_list.count() == channel * 32000
            if hasattr(window, "dbc_loader"):
                assert max(gaps) < 1.0
        # Let the visible view complete its queued batched layout before QA.
        settled = time.perf_counter()
        while time.perf_counter() - settled < 1:
            app.processEvents()
            time.sleep(0.001)
        assert not window.signal_list.visualItemRect(window.signal_list.item(0)).isEmpty()
        window.grab().save(str(root / f"{args.label}.png"))
    finally:
        timer.stop()
        window.project_dirty = False
        window.close()
    (root / f"{args.label}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results), flush=True)


if __name__ == "__main__":
    main()
