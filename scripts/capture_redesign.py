"""Render the redesigned native UI using generated, non-sensitive demo data."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui, QtWidgets

from canflow.app import MainWindow
from canflow.core import available_signals, load_databases, prepare_sequence
from scripts.capture_screenshot import make_demo


def capture(output: Path) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyle("Fusion")
    font_path = Path(r"C:\Windows\Fonts\msyh.ttc")
    if font_path.exists():
        QtGui.QFontDatabase.addApplicationFont(str(font_path))
        app.setFont(QtGui.QFont("Microsoft YaHei UI", 10))
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="canflow-design-") as directory:
        blf, dbc = make_demo(Path(directory))
        window = MainWindow()
        try:
            window.resize(1600, 1024)
            window.show()
            window._set_playback_details_visible(False, persist=False)
            window._scan_done(prepare_sequence([blf]))
            window.mapping = {1: dbc}
            window._refresh_channel_table()
            window.signals = available_signals(load_databases(window.mapping))
            window._populate_signals()
            for row in range(window.signal_list.count()):
                window.signal_list.item(row).setCheckState(QtCore.Qt.CheckState.Checked)
            window.signal_timer.stop()
            window.selected = set(window.signals)
            window._sync_curves()
            window._start()
            deadline = time.monotonic() + 10
            while window.worker is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.01)
            assert window.worker is None, "Demo replay did not finish"
            assert all(len(curve.xData) for curve in window.curves.values())
            window.project_path = Path("制动系统验证.canflow.json")
            window._update_window_title()
            window.follow.setChecked(False)
            window.wave_splitter.setSizes([320, 1240])
            window.plot.setXRange(0, 30, padding=0)
            window._refresh_plot()
            window._update_cursor_values(15)
            window.chart.cursor.setPos(15)
            window.chart.cursor.show()
            window.chart.focus(next(key for key in window.signals if key.name == "BrakePressure"))
            window._set_measurement_enabled(True)
            window._measure_at(14.25)
            window._measure_at(15)
            for width, height in ((1600, 1024), (1280, 800)):
                window.resize(width, height)
                for page, name in ((0, "waveform"), (1, "configuration")):
                    window.pages.setCurrentIndex(page)
                    app.processEvents()
                    assert window.width() == width, "Controls force the window wider than the viewport"
                    assert window.grab().save(str(output / f"{name}-{width}.png"))
        finally:
            window.project_dirty = False
            window.close()


if __name__ == "__main__":
    capture(Path(__file__).resolve().parents[1] / "docs/design/2026-10-25/implemented")
