"""Integration checks for real multi-window imports, replay and lifecycle."""
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import can
import pytest
from PySide6 import QtCore, QtTest, QtWidgets

from canflow.app import MainWindow
from canflow.core import prepare_sequence
from canflow.persistence import SignalGroupStore, load_project
from canflow.workers import ReplayWorker


def wait_for(app, predicate):
    deadline = time.monotonic() + 8
    while not predicate():
        app.processEvents()
        assert time.monotonic() < deadline, "Background task did not settle"
        time.sleep(0.005)
    app.processEvents()


@pytest.fixture
def workspace(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    for session in window.sessions:
        session.settings = QtCore.QSettings(str(tmp_path / "preferences.ini"), QtCore.QSettings.Format.IniFormat)
        session.group_store = SignalGroupStore(tmp_path / "groups.json")
        session.signal_groups = []
    window.show()
    app.processEvents()
    yield app, window
    for session in window.sessions:
        session.project_dirty = False
    window.close()
    app.processEvents()


def prepare_session(app, session, blf, dbc):
    session._scan_done(prepare_sequence([blf]))
    session._assign_dbc_path(dbc, 1)
    wait_for(app, lambda: session.dbc_loader is None)
    for row in range(session.signal_list.count()):
        item = session.signal_list.item(row)
        if item.data(QtCore.Qt.ItemDataRole.UserRole).name == "BatterySOC":
            item.setCheckState(QtCore.Qt.CheckState.Checked)
    session._apply_signals_without_backfill()
    session._update_workflow()


def test_two_real_recordings_replay_into_independent_caches(workspace, sample_recording, tmp_path):
    app, window = workspace
    blf, dbc = sample_recording
    other_blf = tmp_path / "other.blf"
    with can.BLFWriter(other_blf) as writer:
        for index in range(10):
            writer.on_message_received(can.Message(timestamp=1700000100 + index * .1, channel=1,
                arbitration_id=100, is_extended_id=False, data=[0, 0, 0, 100 + index, 0, 0, 0, 0]))
    first, second = window.sessions
    prepare_session(app, first, blf, dbc)
    prepare_session(app, second, other_blf, dbc)
    assert first.cache_path != second.cache_path
    assert first.mapping is not second.mapping
    window.activate_session(first)
    first.start_button.click()
    window.activate_session(second)
    second.start_button.click()
    wait_for(app, lambda: first.worker is None and second.worker is None)
    assert first.db.execute("SELECT COUNT(*), MIN(value), MAX(value) FROM samples").fetchone() == (20, 1, 20)
    assert second.db.execute("SELECT COUNT(*), MIN(value), MAX(value) FROM samples").fetchone() == (10, 100, 109)
    assert first.progress.value() == second.progress.value() == 1000
    first._update_cursor_values(.5)
    second._update_cursor_values(.2)
    assert first.curve_table.item(0, 2).text() == "6 %"
    assert second.curve_table.item(0, 2).text() == "102 %"
    first.measure_toggle.setChecked(True)
    first._measure_at(.5)
    first._measure_at(.7)
    assert first.measure_b is not None and second.measure_b is None
    second._clear_recordings()
    assert first.raw_table.rowCount() == 20
    assert first.measure_b is not None
    assert first.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 20


def test_click_focus_keyboard_and_editors_route_to_owner(workspace):
    app, window = workspace
    first, second = window.sessions
    QtTest.QTest.mouseClick(second.title_label, QtCore.Qt.MouseButton.LeftButton)
    assert window.active_session is second
    assert window.command_stack.currentWidget() is second.commands
    second.measure_toggle.click()
    assert second.chart.measurement_enabled and not first.chart.measurement_enabled
    first.show_editor("signals")
    QtTest.QTest.mouseClick(first.signal_search, QtCore.Qt.MouseButton.LeftButton)
    assert window.active_session is first
    first.signals_dialog.close()
    window.next_window()
    assert window.active_session is second
    window.window_selector.setCurrentIndex(0)
    assert window.active_session is first
    first.display_toggle.click()
    assert first.display_panel.isVisible() and not second.display_panel.isVisible()
    first.display_panel.close()
    assert first.chart.isVisible() and second.chart.isVisible()
    assert first.commands.parentWidget() is window.command_stack


def test_top_import_only_changes_active_window(workspace, sample_recording, monkeypatch):
    app, window = workspace
    blf, _ = sample_recording
    first, second = window.sessions
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileNames", lambda *args: ([str(blf)], ""))
    window.activate_session(second)
    second.import_button.click()
    wait_for(app, lambda: second.scanner is None)
    assert second.files[0].path == blf
    assert first.files == []
    assert second.start_button.isEnabled()
    assert not first.start_button.isEnabled()
    second._start()
    wait_for(app, lambda: second.worker is None)
    assert not second.project_dirty  # Replaying raw frames does not edit the project.


def test_pause_stop_and_close_only_affect_current_worker(workspace, sample_recording, monkeypatch):
    app, window = workspace
    blf, dbc = sample_recording
    first, second = window.sessions
    for session in (first, second):
        prepare_session(app, session, blf, dbc)
    original = ReplayWorker.__init__

    def paused(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.set_paused(True)

    monkeypatch.setattr(ReplayWorker, "__init__", paused)
    first._start()
    second._start()
    workers = first.worker, second.worker
    first.pause_button.setText("继续")
    second.pause_button.setText("继续")
    window.activate_session(first)
    first.pause_button.click()
    wait_for(app, lambda: first.worker is None)
    assert not workers[1]._resume.is_set() and workers[1].isRunning()
    first_cache, second_cache = first.cache_path, second.cache_path
    second.project_dirty = False
    window.activate_session(second)
    assert window.close_active_window()
    assert not workers[1].isRunning()
    assert not second_cache.exists() and first_cache.exists()
    assert window.active_session is first and len(window.sessions) == 1
    assert first.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 20
    assert not window.close_active_window()


def test_cancel_close_keeps_all_sessions_and_project_save_routes(workspace, sample_recording, tmp_path, monkeypatch):
    app, window = workspace
    blf, dbc = sample_recording
    first, second = window.sessions
    prepare_session(app, first, blf, dbc)
    first.project_path = tmp_path / "first.canflow.json"
    second.project_path = tmp_path / "second.canflow.json"
    window.activate_session(first)
    window._project_command("_save_project")
    assert load_project(first.project_path).selected
    assert not second.project_path.exists()
    first.project_dirty = second.project_dirty = True
    answers = iter([QtWidgets.QMessageBox.StandardButton.Discard, QtWidgets.QMessageBox.StandardButton.Cancel])
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a: next(answers))
    assert not window.close()
    assert all(not s._closing and s.cache_path.exists() for s in window.sessions)
    assert window.isVisible()
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a: QtWidgets.QMessageBox.StandardButton.Cancel)
    assert not window.close_active_window()
    assert len(window.sessions) == 2


def test_group_updates_from_two_windows_do_not_overwrite_each_other(workspace, sample_recording, monkeypatch):
    app, window = workspace
    blf, dbc = sample_recording
    first, second = window.sessions
    for session in (first, second):
        prepare_session(app, session, blf, dbc)
    names = iter(["第一组", "第二组"])
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", lambda *a, **k: (next(names), True))
    first._save_signal_group()
    second._save_signal_group()
    assert {group.name for group in first.group_store.load()} == {"第一组", "第二组"}
    first.show_editor("signals")
    assert first.group_combo.count() == 2


def test_many_windows_scroll_and_ids_are_not_reused(workspace):
    app, window = workspace
    for _ in range(3):
        window.add_analysis_window()
    window.resize(1000, 660)
    app.processEvents()
    assert window.width() == 1000 and window.height() == 660
    assert window.scroll.verticalScrollBar().maximum() > 0
    assert window.close_active_window()
    added = window.add_analysis_window()
    assert added.session_id == 6
    assert window.active_session is added
    assert len({s.cache_path for s in window.sessions}) == len(window.sessions)


def test_queued_import_results_cannot_reopen_closed_cache(workspace, sample_recording, monkeypatch):
    app, window = workspace
    blf, dbc = sample_recording
    first, second = window.sessions
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileNames", lambda *args: ([str(blf)], ""))
    second._add_files()
    assert second.scanner.wait(5000)
    old_cache = second.cache_path
    window.activate_session(second)
    assert window.close_active_window()
    app.processEvents()
    assert not old_cache.exists()
    assert first.files == []
    third = window.add_analysis_window()
    third._assign_dbc_path(dbc, 1)
    assert third.dbc_loader.wait(5000)
    third_cache = third.cache_path
    assert window.close_active_window()
    app.processEvents()
    assert not third_cache.exists()
    assert first.mapping == {}


def test_failed_replay_remains_failed_and_other_window_stays_ready(workspace, tmp_path, monkeypatch):
    from canflow.core import FileInfo
    app, window = workspace
    first, second = window.sessions
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args))
    first.files = [FileInfo(tmp_path / "missing.blf", 1000, 1010, 1, (1,))]
    first._start()
    wait_for(app, lambda: first.worker is None)
    assert errors and first.replay_badge.text() == "回放失败"
    assert "回放失败" in first.statusBar().currentMessage()
    assert second.replay_badge.text() == "等待导入"
    assert first.start_button.isEnabled()
