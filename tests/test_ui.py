import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import can
from PySide6 import QtCore, QtWidgets

from canflow.app import MainWindow
from canflow.core import available_signals, load_databases, prepare_sequence


def test_window_replays_selected_signal(tmp_path: Path) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    blf = tmp_path / "one.blf"
    with can.BLFWriter(blf) as writer:
        for timestamp in (1_700_000_000, 1_700_000_001):
            writer.on_message_received(can.Message(
                timestamp=timestamp, channel=1, arbitration_id=0x123,
                data=[10], is_extended_id=False,
            ))
    dbc = tmp_path / "one.dbc"
    dbc.write_text(
        'VERSION ""\nNS_ :\nBS_: \nBU_: ECU\n'
        'BO_ 291 Example: 1 ECU\n SG_ Speed : 0|8@1+ (0.5,0) [0|127.5] "km/h" ECU\n',
        encoding="utf-8",
    )
    window = MainWindow()
    try:
        window._scan_done(prepare_sequence([blf]))
        window.mapping = {1: dbc}
        window.signals = available_signals(load_databases(window.mapping))
        window._populate_signals()
        window.signal_list.item(0).setCheckState(QtCore.Qt.CheckState.Checked)
        window._start()
        deadline = time.monotonic() + 5
        while window.worker is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        assert window.worker is None
        assert window.progress.value() == 1000
        assert window.raw_table.rowCount() == 2
        assert window.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 2
    finally:
        window.close()


def test_recorded_battery_soc_is_drawn(sample_recording: tuple[Path, Path]) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    blf, dbc = sample_recording
    window = MainWindow()
    try:
        window._scan_done(prepare_sequence([blf]))
        window.mapping = {1: dbc}
        window.signals = available_signals(load_databases(window.mapping))
        window._populate_signals()
        for row in range(window.signal_list.count()):
            item = window.signal_list.item(row)
            if item.data(QtCore.Qt.ItemDataRole.UserRole).name == "BatterySOC":
                item.setCheckState(QtCore.Qt.CheckState.Checked)
                break
        else:
            raise AssertionError("DBC 中没有 BatterySOC")
        window._start()
        deadline = time.monotonic() + 5
        while window.worker is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        window._refresh_plot()
        key = next(iter(window.selected))
        curve = window.curves[key]
        assert len(curve.xData) > 0
        low, high = window.plot.viewRange()[0]
        assert any(low <= point <= high for point in curve.xData)

        # Selecting a decodable signal after playback should backfill it automatically.
        for row in range(window.signal_list.count()):
            item = window.signal_list.item(row)
            if item.data(QtCore.Qt.ItemDataRole.UserRole).name == "InvTemp":
                item.setCheckState(QtCore.Qt.CheckState.Checked)
                break
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            app.processEvents()
            if any(signal.name == "InvTemp" for signal in window.cached):
                break
            time.sleep(0.01)
        window._refresh_plot()
        added = next(signal for signal in window.selected if signal.name == "InvTemp")
        assert len(window.curves[added].xData) > 0

        # The DBC puts MotorSpeed beyond the 8-byte frames in this BLF.
        for row in range(window.signal_list.count()):
            item = window.signal_list.item(row)
            if item.data(QtCore.Qt.ItemDataRole.UserRole).name == "MotorSpeed":
                item.setCheckState(QtCore.Qt.CheckState.Checked)
                break
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            app.processEvents()
            if "MotorSpeed" in window.signal_notice.text():
                break
            time.sleep(0.01)
        assert "14 字节" in window.signal_notice.text()
        assert "8 字节" in window.signal_notice.text()
    finally:
        window.close()
