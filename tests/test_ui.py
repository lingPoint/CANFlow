import os
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import can
import pytest
from PySide6 import QtCore, QtWidgets

from canflow import __version__
from canflow.app import MainWindow, cursor_clock_text
from canflow.chart import MultiSignalChart
from canflow.core import FileInfo, SignalKey
from canflow.core import available_signals, load_databases, prepare_sequence
from canflow.persistence import SignalReference, load_project, save_project
from canflow.workers import ReplayWorker


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
        window.follow.setChecked(True)
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
        assert window.plot.viewRange()[0][1] < 2
    finally:
        window.close()


def test_window_replays_out_of_order_multichannel_timestamps(tmp_path: Path) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    base = 1_790_336_051.0
    blf = tmp_path / "multichannel.blf"
    with can.BLFWriter(blf) as writer:
        for timestamp, channel, value in (
            (base + 0.1, 1, 10),
            (base + 1.0, 1, 11),
            (base + 0.999997, 2, 12),
            (base + 0.5, 2, 13),
        ):
            writer.on_message_received(can.Message(
                timestamp=timestamp, channel=channel, arbitration_id=0x123,
                data=[value], is_extended_id=False,
            ))
    dbc = tmp_path / "one.dbc"
    dbc.write_text(
        'VERSION ""\nNS_ :\nBS_: \nBU_: ECU\n'
        'BO_ 291 Example: 1 ECU\n SG_ Speed : 0|8@1+ (1,0) [0|255] "km/h" ECU\n',
        encoding="utf-8",
    )
    window = MainWindow()
    try:
        assert f"v{__version__}" in window.windowTitle()
        assert any(label.text() == f"CANFlow v{__version__}" for label in window.findChildren(QtWidgets.QLabel))
        info = prepare_sequence([blf])[0]
        window._scan_done([info])
        window.mapping = {1: dbc, 2: dbc}
        window.signals = available_signals(load_databases(window.mapping))
        window._populate_signals()
        for row in range(window.signal_list.count()):
            window.signal_list.item(row).setCheckState(QtCore.Qt.CheckState.Checked)
        window._start()
        deadline = time.monotonic() + 5
        while window.worker is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        window._refresh_plot()
        assert window.worker is None
        assert window.replay_badge.text() == "回放完成"
        assert window.progress.value() == 1000
        assert window.raw_table.rowCount() == 4
        assert window.playhead == info.last
        assert window.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 4
        assert all(len(curve.xData) > 0 for curve in window.curves.values())
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


def test_failed_replay_does_not_start_backfill(tmp_path: Path) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    try:
        key = SignalKey(1, 0x123, False, "Speed")
        window.files = [FileInfo(tmp_path / "missing.blf", 1000, 1010, 2, (1,))]
        window.selected = {key}
        window.played_until = 1005
        worker = ReplayWorker(window.files, {}, {key}, tmp_path / "samples.sqlite")
        window.worker = worker
        window._worker_finished(worker)
        assert window.worker is None
    finally:
        window.close()
        app.processEvents()


def test_chart_scales_independent_y_ranges_together() -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    chart = MultiSignalChart()
    first = SignalKey(1, 1, False, "Small")
    second = SignalKey(1, 2, False, "Large")
    chart.add_signal(first, first.label(), "V")
    chart.add_signal(second, second.label(), "rpm")
    assert chart.visible_axis_keys() == [first, second]
    chart.resize(900, 500)
    chart.show()
    app.processEvents()
    chart._sync_geometry()
    first_lane = chart.layers[first].view.geometry()
    second_lane = chart.layers[second].view.geometry()
    assert first_lane.bottom() <= second_lane.top() or second_lane.bottom() <= first_lane.top()
    chart.set_data(first, [0, 1], [0, 10])
    chart.set_data(second, [0, 1], [1000, 5000])
    before = {key: chart.y_range(key) for key in (first, second)}
    chart.scale_visible_y(0.5)
    after = {key: chart.y_range(key) for key in (first, second)}
    for key in (first, second):
        old_width = before[key][1] - before[key][0]
        new_width = after[key][1] - after[key][0]
        assert new_width == pytest.approx(old_width * 0.5)
    assert before[first] != before[second]
    chart.close()
    app.processEvents()


def test_auto_y_immediately_restores_existing_curves() -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    chart = MultiSignalChart()
    try:
        keys = [SignalKey(1, index, False, f"Signal{index}") for index in range(21)]
        for index, key in enumerate(keys):
            chart.add_signal(key, key.label(), "")
            chart.set_data(key, [0, 1], [index * 100, index * 100 + 10])
        expected = {key: chart.y_range(key) for key in keys}
        chart.scale_visible_y(0.2)
        chart.y_locked = True
        chart.reset_auto_scale()
        assert not chart.y_locked
        for key in keys:
            assert chart.y_range(key) == pytest.approx(expected[key])
    finally:
        chart.close()
        app.processEvents()


def test_auto_y_button_fits_all_tracks_in_current_time_window(tmp_path: Path) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    try:
        window.plot_timer.stop()
        window.settings = QtCore.QSettings(str(tmp_path / "settings.ini"), QtCore.QSettings.Format.IniFormat)
        window.files = [FileInfo(tmp_path / "sample.blf", 1000, 1010, 4, (1,))]
        window.follow.setChecked(False)
        keys = [SignalKey(1, index, False, f"Signal{index}") for index in range(21)]
        for key in keys:
            window.curves[key] = window.chart.add_signal(key, key.label(), "")
            window.db.executemany("INSERT INTO samples VALUES (?, ?, ?)", [
                (key.storage_key(), 1000, 0), (key.storage_key(), 1001, 100),
                (key.storage_key(), 1005, 20), (key.storage_key(), 1006, 30),
            ])
            window.chart.set_data(key, [0, 1], [0, 100])
        window.db.commit()
        window.chart.scale_visible_y(0.2)
        window.lock_y.setChecked(True)
        window.plot.setXRange(5, 6, padding=0)
        button = next(button for button in window.findChildren(QtWidgets.QPushButton)
                      if button.text() == "自动适配 Y")
        button.click()
        assert not window.lock_y.isChecked()
        assert not window.chart.y_locked
        assert window.plot.viewRange()[0] == pytest.approx([5, 6])
        for key in keys:
            assert window.chart.y_range(key) == pytest.approx([19.2, 30.8])
    finally:
        window.close()
        app.processEvents()


def test_fit_xy_keeps_full_capture_range_and_rescales_each_track(tmp_path: Path) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    try:
        window.plot_timer.stop()
        origin = 1_700_000_000
        window.files = [FileInfo(tmp_path / "sample.blf", origin, origin + 60, 4, (1,))]
        keys = [SignalKey(1, index, False, f"Speed{index}") for index in range(2)]
        for index, key in enumerate(keys):
            window.curves[key] = window.chart.add_signal(key, key.label(), "km/h")
            window.db.executemany("INSERT INTO samples VALUES (?, ?, ?)", [
                (key.storage_key(), origin + 5, index * 10 + 1),
                (key.storage_key(), origin + 25, index * 10 + 9),
            ])
        window.db.commit()
        window.plot.setXRange(0, 60, padding=0)
        window.chart.scale_visible_y(0.2)
        window._fit_xy()
        x_low, x_high = window.plot.viewRange()[0]
        assert x_low <= 0 < 5 < 25 < 60 <= x_high
        for index, key in enumerate(keys):
            y_low, y_high = window.chart.y_range(key)
            assert y_low < index * 10 + 1 < index * 10 + 9 < y_high
        assert not window.chart.y_locked
    finally:
        window.close()
        app.processEvents()


def test_capture_time_axis_formats_original_blf_timestamp() -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    chart = MultiSignalChart()
    try:
        origin = datetime(2026, 9, 24, 21, 47).timestamp()
        chart.set_capture_origin(origin)
        chart.set_time_mode("capture")
        axis = chart.widget.getPlotItem().getAxis("bottom")
        assert "2026-09-24" in axis.labelText
        assert axis.tickStrings([0, 60], 1, 60) == ["21:47:00", "21:48:00"]
        assert axis.tickStrings([3 * 3600], 1, 60) == ["2026-09-25 00:47:00"]
        chart.set_time_mode("relative")
        assert axis.tickStrings([0, 60], 1, 60) == ["0", "60"]
    finally:
        chart.close()
        app.processEvents()


def test_window_opens_project_and_restores_mapping_and_selection(
    tmp_path: Path, sample_recording: tuple[Path, Path]
) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _blf, dbc = sample_recording
    key = SignalKey(1, 0x64, False, "BatterySOC")
    project = tmp_path / "demo.canflow.json"
    save_project(project, {1: dbc}, {key})
    window = MainWindow()
    try:
        window._open_project(project)
        assert window.project_path == project
        assert window.mapping == {1: dbc}
        assert window.selected == {key}
        assert key in window.curves
        assert window.project_dirty is False
    finally:
        window.close()


def test_unresolved_signal_can_be_removed_and_marks_project_dirty_immediately(tmp_path: Path) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    key = SignalKey(1, 0x123, False, "Speed")
    project = tmp_path / "missing.canflow.json"
    save_project(project, {}, (), unresolved_selected=(SignalReference(key, "old-definition"),))
    window = MainWindow()
    try:
        window._open_project(project)
        assert key in window.unresolved_selected
        item = window.signal_list.item(0)
        assert item.flags() & QtCore.Qt.ItemFlag.ItemIsEnabled
        item.setCheckState(QtCore.Qt.CheckState.Unchecked)
        assert window.project_dirty
        assert window._save_project()
        assert load_project(project).selected == ()
        app.processEvents()
        assert not window.project_dirty
    finally:
        window.close()
        app.processEvents()


def test_replacing_dbc_preserves_incompatible_selection_as_unresolved(tmp_path: Path) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    first = tmp_path / "first.dbc"
    second = tmp_path / "second.dbc"
    template = (
        'VERSION ""\nNS_ :\nBS_: \nBU_: ECU\n'
        'BO_ 291 Example: 1 ECU\n SG_ Speed : 0|8@1+ ({scale},0) [0|255] "km/h" ECU\n'
    )
    first.write_text(template.format(scale=1), encoding="utf-8")
    second.write_text(template.format(scale=2), encoding="utf-8")
    key = SignalKey(1, 0x123, False, "Speed")
    window = MainWindow()
    try:
        window._assign_dbc_path(first, 1)
        window.selected = {key}
        window._populate_signals()
        window._assign_dbc_path(second, 1)
        assert key not in window.selected
        assert key in window.unresolved_selected
        assert window.signal_list.count() == 2
        old_item = window.signal_list.item(1)
        assert old_item.text().startswith("[未解析]")
        old_item.setCheckState(QtCore.Qt.CheckState.Unchecked)
        window._apply_signals()
        assert key not in window.unresolved_selected
    finally:
        window.close()
        app.processEvents()


def test_playback_details_panel_can_be_hidden_and_restored() -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    try:
        window._set_playback_details_visible(False, persist=False)
        assert window.playback_details.isHidden()
        assert window.details_toggle.text() == "显示回放详情"
        window._set_playback_details_visible(True, persist=False)
        assert not window.playback_details.isHidden()
        assert window.details_toggle.text() == "隐藏回放详情"
    finally:
        window.close()
        app.processEvents()


def test_cursor_readout_and_separate_configuration_page(tmp_path: Path) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    try:
        key = SignalKey(1, 0x123, False, "Speed")
        window.files = [FileInfo(tmp_path / "sample.blf", 1000, 1010, 2, (1,))]
        window.selected = {key}
        window._sync_curves()
        window.db.execute("INSERT INTO samples VALUES (?, ?, ?)", (key.storage_key(), 1001, 20))
        window.db.commit()
        window.plot.setXRange(0, 10, padding=0)
        window._update_cursor_values(1.0)
        assert window.cursor_time_label.text() == cursor_clock_text(1001)
        assert window.cursor_absolute_label.text()
        assert window.cursor_readout.parentWidget() is window.chart.parentWidget()
        assert window.cursor_readout.toolTip() == window.cursor_absolute_label.text()
        assert window.sidebar.findChildren(QtWidgets.QWidget, "timeCard") == []
        assert window.curve_table.item(0, 1).text() == "Speed"
        assert window.curve_table.item(0, 2).text() == "20"
        assert window.pages.currentIndex() == 0
        assert window.curve_table.parentWidget() is window.sidebar
        window.pages.setCurrentIndex(1)
        assert window.file_table.isVisibleTo(window.pages)
        assert window.signal_list.isVisibleTo(window.pages)
        assert not window.chart.isVisibleTo(window.pages)
        window.pages.setCurrentIndex(0)
        assert window.chart.isVisibleTo(window.pages)
    finally:
        window.close()
        app.processEvents()


def test_cursor_clock_uses_capture_time_with_milliseconds() -> None:
    timestamp = datetime(2026, 5, 6, 22, 38, 11, 11_000).timestamp()
    assert cursor_clock_text(timestamp) == "22点38分11秒11毫秒"


def test_forty_selected_signals_have_searchable_cursor_values(tmp_path: Path) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    try:
        window.plot_timer.stop()
        window.files = [FileInfo(tmp_path / "sample.blf", 1000, 1010, 40, (1,))]
        keys = [SignalKey(1, index, False, f"Signal{index:02d}") for index in range(40)]
        window.selected = set(keys)
        window._sync_curves()
        window.db.executemany("INSERT INTO samples VALUES (?, ?, ?)", [
            (key.storage_key(), 1001, index) for index, key in enumerate(keys)
        ])
        window.db.commit()
        window.plot.setXRange(0, 10, padding=0)
        window._update_cursor_values(1.0)
        assert window.curve_table.rowCount() == 40
        assert sum(layer.visible for layer in window.chart.layers.values()) == 3
        last_row = next(row for row in range(40) if window.curve_table.item(row, 1).text() == "Signal39")
        assert window.curve_table.item(last_row, 2).text() == "39"
        window.curve_search.setText("Signal39")
        assert sum(not window.curve_table.isRowHidden(row) for row in range(40)) == 1
        assert "1 / 40" in window.curve_footer.text()
        assert window.display_panel.isHidden()
        window.display_toggle.click()
        assert not window.display_panel.isHidden()
    finally:
        window.close()
        app.processEvents()


def test_display_settings_toggle_stays_visible_in_short_window() -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    try:
        window.show()
        window.resize(1114, 700)
        window.display_toggle.click()
        app.processEvents()
        assert window.height() == 700
        assert window.display_panel.isVisible()
        assert window.display_toggle.geometry().bottom() < window.sidebar.height()
        assert window.display_panel.geometry().bottom() < window.sidebar.height()

        window.display_toggle.click()
        app.processEvents()
        assert window.display_toggle.isVisible()
        assert window.display_toggle.geometry().bottom() < window.sidebar.height()
    finally:
        window.close()
        app.processEvents()
