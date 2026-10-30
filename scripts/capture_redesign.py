"""Capture the production multi-window workspace with generated CAN recordings."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import can
from PySide6 import QtCore, QtGui, QtWidgets

from canflow.app import MainWindow
from canflow.core import available_signals, load_databases, prepare_sequence
from scripts.capture_screenshot import make_demo


def capture(output: Path) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyle("Fusion")
    font = Path(r"C:\Windows\Fonts\msyh.ttc")
    if font.exists():
        font_id = QtGui.QFontDatabase.addApplicationFont(str(font))
        families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
        app.setFont(QtGui.QFont(families[0] if families else "Microsoft YaHei", 9))
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="canflow-workspace-") as directory:
        root = Path(directory)
        # Keep captures away from the user's real preferences.
        previous_format = QtCore.QSettings.defaultFormat()
        QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
        QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope, str(root))
        window = MainWindow()
        try:
            for i, session in enumerate(window.sessions):
                folder = root / str(i)
                folder.mkdir()
                blf, dbc = make_demo(folder)
                if i:
                    shifted = folder / "afternoon_capture.blf"
                    with can.BLFReader(blf) as reader, can.BLFWriter(shifted) as writer:
                        for message in reader:
                            message.timestamp += 3600
                            message.data[0] -= 12
                            message.data[2] = min(100, message.data[2] + 20)
                            writer.on_message_received(message)
                    blf = shifted
                session._scan_done(prepare_sequence([blf]))
                session.mapping = {1: dbc}
                session._refresh_channel_table()
                session.signals = available_signals(load_databases(session.mapping))
                session.selected = set(session.signals)
                session._populate_signals()
                session._start()
            window.show()
            deadline = time.monotonic() + 10
            while any(s.worker for s in window.sessions):
                app.processEvents()
                assert time.monotonic() < deadline, "Demo playback did not finish"
                time.sleep(.01)
            for i, session in enumerate(window.sessions):
                session.follow.setChecked(False)
                session.plot.setXRange(0, 30, padding=0)
                session._refresh_plot(all_visible=True)
                assert all(len(curve.xData) for curve in session.curves.values())
                session._update_cursor_values(15 if i == 0 else 21)
                session.chart.cursor.setPos(15 if i == 0 else 21)
                session.chart.cursor.show()
                session.project_path = Path("制动系统验证.canflow.json" if i == 0 else "温升记录.canflow.json")
                session.project_dirty = False
                session._update_window_title()
            first = window.sessions[0]
            first.chart.focus(next(key for key in first.signals if key.name == "BrakePressure"))
            first.measure_toggle.setChecked(True)
            first._measure_at(14.25)
            first._measure_at(15)
            window.activate_session(first)
            for width, height in ((1600, 1024), (1280, 800), (1000, 660)):
                window.resize(width, height)
                app.processEvents()
                assert window.width() == width and window.height() == height
                assert window.grab().save(str(output / f"workspace-{width}.png"))
            first.show_editor("mapping")
            app.processEvents()
            assert first.mapping_dialog.grab().save(str(output / "dbc-editor.png"))
            first.mapping_dialog.close()
            first.show_editor("signals")
            app.processEvents()
            assert first.signals_dialog.grab().save(str(output / "signal-editor.png"))
        finally:
            for session in window.sessions:
                session.project_dirty = False
            window.close()
            app.processEvents()
            QtCore.QSettings.setDefaultFormat(previous_format)


if __name__ == "__main__":
    capture(Path(__file__).resolve().parents[1] / "docs/design/2026-10-30")
