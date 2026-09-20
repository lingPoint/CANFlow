"""Capture the real CANFlow window with generated, non-sensitive CAN data."""

from __future__ import annotations

import math
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import can
from PySide6 import QtCore, QtGui, QtWidgets

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from canflow.app import MainWindow
from canflow.core import available_signals, load_databases, prepare_sequence


def make_demo(folder: Path) -> tuple[Path, Path]:
    dbc = folder / "demo_signals.dbc"
    dbc.write_text(
        'VERSION ""\nNS_ :\nBS_: \nBU_: ECU\n'
        'BO_ 256 VehicleStatus: 8 ECU\n'
        ' SG_ BatterySOC : 0|8@1+ (1,0) [0|100] "%" ECU\n'
        ' SG_ InverterTemp : 8|8@1+ (1,0) [0|150] "degC" ECU\n'
        ' SG_ BrakePressure : 16|8@1+ (1,0) [0|100] "bar" ECU\n',
        encoding="utf-8",
    )
    blf = folder / "demo_capture.blf"
    start = datetime(2026, 9, 20, 9, 0, 0).timestamp()
    with can.BLFWriter(blf) as writer:
        for index in range(301):
            seconds = index / 10
            soc = round(56 + 12 * seconds / 30)
            temperature = round(35 + 9 * math.sin(seconds / 3))
            pressure = round(12 + 28 * ((seconds % 7) / 7))
            writer.on_message_received(can.Message(
                timestamp=start + seconds,
                channel=1,
                arbitration_id=0x100,
                data=[soc, temperature, pressure, 0, 0, 0, 0, 0],
                is_extended_id=False,
            ))
    return blf, dbc


def capture(output: Path) -> None:
    app = QtWidgets.QApplication([])
    font = Path(r"C:\Windows\Fonts\msyh.ttc")
    if font.exists():
        QtGui.QFontDatabase.addApplicationFont(str(font))
        app.setFont(QtGui.QFont("Microsoft YaHei UI", 9))
    app.setStyle("Fusion")
    with tempfile.TemporaryDirectory(prefix="canflow-screenshot-") as directory:
        blf, dbc = make_demo(Path(directory))
        window = MainWindow()
        try:
            window.resize(1600, 920)
            window.show()
            splitter = window.findChild(QtWidgets.QSplitter)
            splitter.setSizes([440, 1160])
            window.file_table.setColumnWidth(0, 170)
            window.file_table.setColumnWidth(1, 140)
            window.file_table.setColumnWidth(2, 60)
            window._scan_done(prepare_sequence([blf]))
            window.mapping = {1: dbc}
            window.channel_table.item(0, 1).setText(dbc.name)
            window.signals = available_signals(load_databases(window.mapping))
            window._populate_signals()
            for row in range(window.signal_list.count()):
                window.signal_list.item(row).setCheckState(QtCore.Qt.CheckState.Checked)
            window._start()
            deadline = time.monotonic() + 10
            while window.worker is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            if window.worker is not None:
                raise RuntimeError("Demo replay did not finish")
            window.follow.setChecked(False)
            window.plot.setXRange(0, 30, padding=0)
            window.plot.setYRange(0, 80, padding=0)
            window._refresh_plot()
            app.processEvents()
            if any(len(curve.xData) == 0 for curve in window.curves.values()):
                raise RuntimeError("A selected demo signal has no plotted points")
            if window.raw_table.rowCount() != 301:
                raise RuntimeError("The raw frame table does not show the full demo capture")
            output.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(output), "PNG"):
                raise RuntimeError(f"Could not save {output}")
        finally:
            window.close()


if __name__ == "__main__":
    capture(PROJECT / "docs" / "images" / "canflow-screenshot.png")
