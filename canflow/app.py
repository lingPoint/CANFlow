from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtSvg, QtWidgets

from . import __version__
from .chart import signal_color
from .core import (
    FileInfo, SignalKey, available_signals, load_databases, missing_signal_reason,
    signal_definition_fingerprint, signal_unit,
)
from .persistence import (
    DbcReference, SignalGroup, SignalGroupStore, SignalReference, export_signal_group, import_signal_group,
    load_project, make_signal_references, resolve_dbc_reference, save_project,
)
from .store import connect, nearest_sample, plot_points_with_samples
from .workers import DbcLoadWorker, ReplayWorker, ScanCache, ScanWorker
from .workspace import MainWindow


def logo_pixmap(size: int) -> QtGui.QPixmap:
    logo = Path(__file__).resolve().parent / "assets" / "logo-mark.svg"
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    QtSvg.QSvgRenderer(str(logo)).render(painter)
    painter.end()
    return pixmap


def clock_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def cursor_clock_text(timestamp: float) -> str:
    moment = datetime.fromtimestamp(timestamp)
    return f"{moment:%H点%M分%S秒}{moment.microsecond // 1000}毫秒"


def sample_clock_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")


class SignalRowDelegate(QtWidgets.QStyledItemDelegate):
    """Keep the native checkbox/keyboard behavior with aligned signal metadata."""

    def sizeHint(self, option, index):
        return QtCore.QSize(320, 80 if index.data(QtCore.Qt.ItemDataRole.UserRole + 2) else 48)

    def editorEvent(self, event, model, option, index):
        styled = QtWidgets.QStyleOptionViewItem(option)
        if index.data(QtCore.Qt.ItemDataRole.UserRole + 2):
            if isinstance(event, QtGui.QMouseEvent) and event.position().y() < option.rect.top() + 32:
                return False
            styled.rect.adjust(0, 32, 0, 0)
        return super().editorEvent(event, model, styled, index)

    def paint(self, painter, option, index):
        key = index.data(QtCore.Qt.ItemDataRole.UserRole)
        if not isinstance(key, SignalKey):
            return super().paint(painter, option, index)
        styled = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        group = index.data(QtCore.Qt.ItemDataRole.UserRole + 2)
        if group:
            painter.save()
            painter.fillRect(option.rect.adjusted(0, 0, 0, -(option.rect.height() - 30)),
                             QtGui.QColor("#eef3f6"))
            painter.setPen(QtGui.QColor("#657c8d"))
            painter.drawText(option.rect.adjusted(12, 0, -12, -(option.rect.height() - 30)),
                             QtCore.Qt.AlignmentFlag.AlignVCenter, group)
            painter.restore()
            styled.rect.adjust(0, 32, 0, 0)
        styled.text = ""
        style = styled.widget.style() if styled.widget else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.ControlElement.CE_ItemViewItem, styled, painter, styled.widget)
        painter.save()
        rect = styled.rect.adjusted(40, 4, -12, -4)
        unresolved = index.data(QtCore.Qt.ItemDataRole.UserRole + 1)
        checked = index.data(QtCore.Qt.ItemDataRole.CheckStateRole) == QtCore.Qt.CheckState.Checked.value
        painter.setPen(QtGui.QColor("#a56219" if unresolved else signal_color(key) if checked else "#283d4c"))
        name = ("[未解析] " if unresolved else "") + key.name
        name = painter.fontMetrics().elidedText(name, QtCore.Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect.adjusted(0, 0, 0, -22), QtCore.Qt.AlignmentFlag.AlignVCenter, name)
        painter.setPen(QtGui.QColor("#657c8d"))
        unit = index.data(QtCore.Qt.ItemDataRole.UserRole + 3) or ""
        painter.drawText(rect.adjusted(0, 22, 0, 0), QtCore.Qt.AlignmentFlag.AlignVCenter,
                         f"CAN {key.channel}  ·  {'EXT' if key.extended else 'STD'} 0x{key.frame_id:X}"
                         + (f"    ·    {unit}" if unit else ""))
        painter.restore()


class AnalysisWindow(QtWidgets.QMainWindow):
    """One recording sequence, DBC mapping, selection and cache, owned by one workspace card."""
    state_changed = QtCore.Signal()

    def __init__(self, session_id: int = 1) -> None:
        super().__init__()
        self.session_id = session_id
        self._closing = False
        self.setWindowTitle("CANFlow · BLF 波形回放")
        self.setWindowIcon(QtGui.QIcon(logo_pixmap(256)))
        self.resize(1600, 1024)
        self.files: list[FileInfo] = []
        self.mapping: dict[int, Path] = {}
        self.missing_mappings: dict[int, DbcReference] = {}
        self.unconfigured_channels: set[int] = set()
        self.project_path: Path | None = None
        self.project_active = False
        self.project_dirty = False
        self.unresolved_selected: dict[SignalKey, SignalReference] = {}
        self.signals: list[SignalKey] = []
        self.playhead = 0.0
        self.played_until = 0.0
        self.selected: set[SignalKey] = set()
        self.cached: set[SignalKey] = set()
        self.worker: ReplayWorker | None = None
        self._replay_error: str | None = None
        self.scanner: ScanWorker | None = None
        self.scan_cache = ScanCache()
        self.dbc_loader: DbcLoadWorker | None = None
        self._dbc_generation = 0
        self._signal_population = 0
        self._populating_signals = False
        self.cache_dir = tempfile.TemporaryDirectory(prefix="canflow-")
        self.cache_path = Path(self.cache_dir.name) / "samples.sqlite"
        self.db = connect(self.cache_path)
        self.curves: dict[SignalKey, pg.PlotDataItem] = {}
        self.curve_data: dict[SignalKey, tuple[list[float], list[float]]] = {}
        self._plot_versions: dict[SignalKey, tuple] = {}
        self.cursor_x: float | None = None
        self.measure_key: SignalKey | None = None
        self.measure_a: tuple[float, float] | None = None
        self.measure_b: tuple[float, float] | None = None
        self.settings = QtCore.QSettings("CANFlow", "CANFlow")
        config_dir = Path(QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.AppConfigLocation
        ))
        self.group_store = SignalGroupStore(config_dir / "signal-groups.json")
        try:
            self.signal_groups = self.group_store.load()
        except Exception:
            self.signal_groups = []
        self.signal_timer = QtCore.QTimer(self)
        self.signal_timer.setSingleShot(True)
        self.signal_timer.setInterval(150)
        self.signal_timer.timeout.connect(self._apply_signals)
        self._build_ui()
        self._update_window_title()
        self.plot_timer = QtCore.QTimer(self)
        self.plot_timer.setInterval(500)
        self.plot_timer.timeout.connect(self._refresh_plot)
        self.plot_timer.start()
        self.progress_timer = QtCore.QTimer(self)
        self.progress_timer.setInterval(100)
        self.progress_timer.timeout.connect(self._poll_replay)
        self.progress_timer.start()

    def _build_ui(self) -> None:
        from .session_ui import build_session_ui
        build_session_ui(self, SignalRowDelegate)

    def show_editor(self, name: str) -> None:
        editors = {"files": self.files_dialog, "mapping": self.mapping_dialog,
                   "signals": self.signals_dialog, "display": self.display_panel,
                   "details": self.playback_details}
        if name == "signals":
            self._reload_groups()
        editor = editors[name]
        editor.show()
        editor.raise_()
        editor.activateWindow()
        if name == "signals":
            self.signal_search.setFocus()
        if name == "mapping" and self.channel_table.rowCount() and self.channel_table.currentRow() < 0:
            self.channel_table.selectRow(0)

    def _update_workflow(self) -> None:
        busy = self.worker is not None or self.scanner is not None or self.dbc_loader is not None
        self.start_button.setEnabled(bool(self.files) and not busy)
        self.pause_button.setEnabled(self.worker is not None and not self.worker.backfill)
        self.stop_button.setEnabled(busy)
        self.add_files.setEnabled(not busy)
        self.import_button.setEnabled(not busy)
        self.clear_files.setEnabled(bool(self.files) and not busy)
        self.mapping_controls.setEnabled(not busy)
        self.recent_dbc_menu.setEnabled(not busy)
        self.timeline.setEnabled(bool(self.files) and not (self.scanner or self.dbc_loader)
                                 and not (self.worker and self.worker.backfill))
        self.start_button.setText("重新回放" if self.files and self.playhead >= self.files[-1].last
                                  else "开始回放")
        self.file_summary.setText(f"已载入 {len(self.files)} 个文件 · 按采集时间排序"
                                  if self.files else "尚未导入记录文件")
        self.file_badge.setToolTip("\n".join(str(item.path) for item in self.files))
        self.mapping_badge.setText(f"DBC：{len(self.mapping)} 个通道" if self.mapping else "DBC：未配置")
        self.mapping_badge.setToolTip("\n".join(f"CAN {ch} · {path}" for ch, path in self.mapping.items()))
        self.state_changed.emit()

    def _update_window_title(self) -> None:
        name = self.project_path.name if self.project_path else "未命名项目"
        marker = " *" if self.project_dirty else ""
        self.setWindowTitle(f"CANFlow v{__version__} · 分析窗口 {self.session_id} · {name}{marker}")
        self.project_label.setText(name.removesuffix('.canflow.json') + marker)
        self.project_label.setToolTip(str(self.project_path) if self.project_path else name)
        self.state_changed.emit()

    def _reload_groups(self) -> bool:
        try:
            self.signal_groups = self.group_store.load()
        except Exception as exc:
            self.statusBar().showMessage(f"信号组无法读取：{exc}")
            return False
        self._populate_group_combo()
        return True

    def _mark_project_dirty(self) -> None:
        if not self.project_active:
            return
        if not self.project_dirty:
            self.project_dirty = True
            self._update_window_title()

    def _maybe_save_project(self) -> bool:
        if not self.project_dirty:
            return True
        answer = QtWidgets.QMessageBox.question(
            self, "保存项目", f"分析窗口 {self.session_id} 的项目有未保存的修改。是否保存？",
            QtWidgets.QMessageBox.StandardButton.Save
            | QtWidgets.QMessageBox.StandardButton.Discard
            | QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        if answer == QtWidgets.QMessageBox.StandardButton.Cancel:
            return False
        if answer == QtWidgets.QMessageBox.StandardButton.Save:
            return self._save_project()
        return True

    def _new_project(self) -> None:
        if not self._maybe_save_project():
            return
        self._clear()
        self.project_path = None
        self.project_active = True
        self.project_dirty = False
        self._update_window_title()

    def _open_project(self, path: str | Path | None = None) -> None:
        if not self._maybe_save_project():
            return
        if path is None or isinstance(path, bool):
            chosen, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "打开 CANFlow 项目", "", "CANFlow 项目 (*.canflow.json);;JSON (*.json)"
            )
            if not chosen:
                return
            project_path = Path(chosen)
        else:
            project_path = Path(path)
        try:
            document = load_project(project_path)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "项目无法打开", str(exc))
            return
        self._clear()
        self.project_path = project_path.resolve()
        self.project_active = True
        self.missing_mappings = {}
        changed: list[str] = []
        missing: list[str] = []
        for reference in document.mappings:
            resolved, modified = resolve_dbc_reference(reference, self.project_path)
            if resolved is None:
                self.missing_mappings[reference.channel] = reference
                missing.append(f"CH{reference.channel}: {reference.absolute_path}")
            elif modified:
                answer = QtWidgets.QMessageBox.question(
                    self, "DBC 已变化",
                    f"CH{reference.channel} 的 DBC 内容与项目保存时不同：\n{resolved}\n\n使用新版本吗？",
                )
                if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                    self.mapping[reference.channel] = resolved
                    changed.append(f"CH{reference.channel}")
                else:
                    self.missing_mappings[reference.channel] = reference
            else:
                self.mapping[reference.channel] = resolved
        try:
            databases = load_databases(self.mapping)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "DBC 无法读取", str(exc))
            databases = {}
        self.signals = available_signals(databases)
        available = set(self.signals)
        self.selected.clear()
        self.unresolved_selected.clear()
        for reference in document.selected:
            current = signal_definition_fingerprint(reference.key, databases)
            if reference.key in available and (not reference.fingerprint or current == reference.fingerprint):
                self.selected.add(reference.key)
            else:
                self.unresolved_selected[reference.key] = reference
        self._refresh_channel_table()
        self._populate_signals()
        self._sync_curves()
        self.project_dirty = bool(changed)
        self._remember_recent("recent/projects", self.project_path)
        self._update_window_title()
        notices = []
        if missing:
            notices.append("缺失 DBC：" + "；".join(missing))
        if self.unresolved_selected:
            notices.append(f"保留了 {len(self.unresolved_selected)} 个未解析信号")
        if notices:
            self._set_signal_notice("。".join(notices))
        self.statusBar().showMessage(f"已打开项目 {self.project_path.name}")

    def _save_project(self) -> bool:
        if self.project_path is None:
            return self._save_project_as()
        self._apply_signals_without_backfill(mark_dirty=False)
        try:
            save_project(
                self.project_path, self.mapping, self.selected,
                self.missing_mappings.values(), self.unresolved_selected.values(),
            )
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "项目无法保存", str(exc))
            return False
        self.project_dirty = False
        self._remember_recent("recent/projects", self.project_path)
        self._update_window_title()
        self.statusBar().showMessage(f"项目已保存：{self.project_path.name}")
        return True

    def _save_project_as(self) -> bool:
        chosen, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "另存 CANFlow 项目", str(self.project_path or "project.canflow.json"),
            "CANFlow 项目 (*.canflow.json)",
        )
        if not chosen:
            return False
        if not chosen.lower().endswith(".canflow.json"):
            chosen += ".canflow.json"
        self.project_path = Path(chosen).resolve()
        self.project_active = True
        return self._save_project()

    def _recent(self, key: str) -> list[str]:
        value = self.settings.value(key, [])
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]

    def _remember_recent(self, key: str, path: Path) -> None:
        text = str(path.resolve())
        values = [item for item in self._recent(key) if item.casefold() != text.casefold()]
        self.settings.setValue(key, [text, *values][:10])

    def _populate_recent_dbcs(self) -> None:
        self.recent_dbc_menu.clear()
        for item in self._recent("recent/dbcs"):
            action = self.recent_dbc_menu.addAction(item)
            action.setEnabled(Path(item).is_file())
            action.triggered.connect(lambda _checked=False, value=item: self._assign_dbc_path(Path(value)))
        if not self.recent_dbc_menu.actions():
            empty = self.recent_dbc_menu.addAction("暂无")
            empty.setEnabled(False)

    def _populate_group_combo(self) -> None:
        current = self.group_combo.currentText() if hasattr(self, "group_combo") else ""
        if not hasattr(self, "group_combo"):
            return
        self.group_combo.clear()
        self.group_combo.addItems(group.name for group in sorted(self.signal_groups, key=lambda item: item.name.casefold()))
        index = self.group_combo.findText(current)
        if index >= 0:
            self.group_combo.setCurrentIndex(index)

    def _current_group(self) -> SignalGroup | None:
        name = self.group_combo.currentText()
        return next((group for group in self.signal_groups if group.name == name), None)

    def _save_signal_group(self) -> None:
        if not self._reload_groups():
            return
        self._apply_signals_without_backfill()
        if not self.selected:
            QtWidgets.QMessageBox.information(self, "信号组", "请先选择至少一个信号")
            return
        suggested = self.group_combo.currentText()
        name, accepted = QtWidgets.QInputDialog.getText(self, "保存信号组", "名称", text=suggested)
        name = name.strip()
        if not accepted or not name:
            return
        references = list(make_signal_references(self.selected, self.mapping))
        references.extend(item for key, item in self.unresolved_selected.items() if key not in self.selected)
        group = SignalGroup(name, tuple(references))
        existing = next((item for item in self.signal_groups if item.name == name), None)
        if existing is not None:
            answer = QtWidgets.QMessageBox.question(self, "覆盖信号组", f"信号组“{name}”已存在，覆盖吗？")
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            self.signal_groups.remove(existing)
        self.signal_groups.append(group)
        self.group_store.save(self.signal_groups)
        self._populate_group_combo()
        self.group_combo.setCurrentText(name)

    def _delete_signal_group(self) -> None:
        if not self._reload_groups():
            return
        group = self._current_group()
        if group is None:
            return
        answer = QtWidgets.QMessageBox.question(self, "删除信号组", f"确定删除“{group.name}”吗？")
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.signal_groups.remove(group)
        self.group_store.save(self.signal_groups)
        self._populate_group_combo()

    def _apply_signal_group(self, append: bool) -> None:
        if not self._reload_groups():
            return
        if self.dbc_loader:
            self.statusBar().showMessage("请等待 DBC 载入完成后应用信号组")
            return
        group = self._current_group()
        if group is None:
            return
        databases = load_databases(self.mapping)
        available = set(self.signals)
        matched: set[SignalKey] = set()
        missing = 0
        conflicts = 0
        for reference in group.signals:
            if reference.key not in available:
                missing += 1
                continue
            fingerprint = signal_definition_fingerprint(reference.key, databases)
            if reference.fingerprint and fingerprint != reference.fingerprint:
                conflicts += 1
                continue
            matched.add(reference.key)
        chosen = (self.selected | matched) if append else matched
        self.signal_list.blockSignals(True)
        for row in range(self.signal_list.count()):
            item = self.signal_list.item(row)
            key = item.data(QtCore.Qt.ItemDataRole.UserRole)
            is_unresolved = bool(item.data(QtCore.Qt.ItemDataRole.UserRole + 1))
            checked = (append and item.checkState() == QtCore.Qt.CheckState.Checked) if is_unresolved else key in chosen
            item.setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
        self.signal_list.blockSignals(False)
        self._update_config_signal_count()
        self._apply_signals()
        detail = f"已应用 {len(matched)} 个信号"
        if missing or conflicts:
            detail += f"；缺失 {missing}，定义冲突 {conflicts}"
        self._set_signal_notice(detail)

    def _import_signal_group(self) -> None:
        if not self._reload_groups():
            return
        chosen, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "导入信号组", "", "CANFlow 信号组 (*.canflow-signals.json);;JSON (*.json)"
        )
        if not chosen:
            return
        try:
            group = import_signal_group(Path(chosen))
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "无法导入", str(exc))
            return
        existing = next((item for item in self.signal_groups if item.name == group.name), None)
        if existing is not None:
            answer = QtWidgets.QMessageBox.question(self, "重名信号组", f"覆盖“{group.name}”吗？")
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                name, accepted = QtWidgets.QInputDialog.getText(self, "另存为", "新名称", text=group.name + " 副本")
                if not accepted or not name.strip():
                    return
                group = SignalGroup(name.strip(), group.signals)
            else:
                self.signal_groups.remove(existing)
        self.signal_groups.append(group)
        self.group_store.save(self.signal_groups)
        self._populate_group_combo()
        self.group_combo.setCurrentText(group.name)

    def _export_signal_group(self) -> None:
        group = self._current_group()
        if group is None:
            return
        chosen, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出信号组", f"{group.name}.canflow-signals.json", "CANFlow 信号组 (*.canflow-signals.json)"
        )
        if not chosen:
            return
        if not chosen.lower().endswith(".canflow-signals.json"):
            chosen += ".canflow-signals.json"
        export_signal_group(Path(chosen), group)

    def _set_y_locked(self, locked: bool) -> None:
        self.chart.y_locked = locked
        self.settings.setValue("chart/y_locked", locked)
        if locked:
            try:
                databases = load_databases(self.mapping)
            except Exception:
                databases = {}
            ranges = self._saved_y_ranges()
            for key in self.chart.layers:
                fingerprint = signal_definition_fingerprint(key, databases) or "unknown"
                low, high = self.chart.y_range(key)
                ranges[f"{key.storage_key()}|{fingerprint}"] = [low, high]
            self.settings.setValue("chart/locked_ranges", json.dumps(ranges, separators=(",", ":")))

    def _reset_auto_y(self) -> None:
        self.lock_y.setChecked(False)
        self.chart.reset_auto_scale()
        self._refresh_plot(all_visible=True)

    def _fit_xy(self) -> None:
        if not self.files:
            return
        first, last = self.files[0].first, self.files[-1].last
        span = last - first
        padding = max(span * 0.03, 0.5 if span == 0 else 0.05)
        origin = self.files[0].first
        self.plot.setXRange(max(0, first - origin - padding), last - origin + padding, padding=0)
        self.lock_y.setChecked(False)
        self.chart.reset_auto_scale()
        self._refresh_plot(all_visible=True)

    def _set_time_mode(self, _index: int) -> None:
        mode = self.time_mode_combo.currentData()
        self.chart.set_time_mode(mode)
        self.settings.setValue("chart/time_mode", mode)

    def _saved_y_ranges(self) -> dict[str, list[float]]:
        try:
            value = json.loads(self.settings.value("chart/locked_ranges", "{}", type=str))
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _set_sample_emphasis(self, width: float) -> None:
        self.chart.set_sample_emphasis(width)
        self.settings.setValue("chart/sample_emphasis", width)

    def _set_grid(self, visible: bool) -> None:
        self.chart.set_grid(visible)
        self.settings.setValue("chart/grid", visible)

    def _set_theme(self, theme: str) -> None:
        self.chart.set_theme(theme)
        self.settings.setValue("workspace/chart_theme", theme)

    def _set_playback_details_visible(self, visible: bool, persist: bool = True) -> None:
        self.playback_details.setVisible(visible)

    def _set_display_settings_visible(self, visible: bool) -> None:
        self.display_panel.setVisible(visible)

    def _update_curve_summary(self) -> None:
        selected = self.curve_table.rowCount()
        displayed = sum(layer.visible for layer in self.chart.layers.values())
        self.signal_count_label.setText(f"{selected} 个信号")
        self.chart_summary_label.setText(f"{selected} 条已选 · {displayed} 条显示")
        self.curve_footer.setText(
            f"显示 {selected} / {selected} 个信号 · 点击聚焦 · 勾选显示"
            if selected else "暂无信号，请从顶部添加信号"
        )

    def _curve_visibility_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        key = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if isinstance(key, SignalKey):
            visible = item.checkState() == QtCore.Qt.CheckState.Checked
            self.chart.set_visible(key, visible)
            if not visible and self.measure_key == key:
                self._clear_measurement()
            self._update_curve_summary()
            if visible:
                self._refresh_plot()

    def _curve_focus_changed(self, row: int, _column: int, _previous_row: int, _previous_column: int) -> None:
        if row < 0:
            return
        item = self.curve_table.item(row, 0)
        if item is not None:
            key = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if isinstance(key, SignalKey):
                if not self.chart.layers[key].visible:
                    item.setCheckState(QtCore.Qt.CheckState.Checked)
                self.chart.focus(key)
                if self.measure_key is not None and self.measure_key != key:
                    self._clear_measurement()
                if self.cursor_x is not None:
                    self._update_cursor_values(self.cursor_x)

    def _set_measurement_enabled(self, enabled: bool) -> None:
        self.chart.measurement_enabled = enabled
        self._clear_measurement()

    def _clear_measurement(self) -> None:
        self.measure_key = None
        self.measure_a = None
        self.measure_b = None
        self.chart.set_measure_markers(None, None)
        self.measure_label.setText(
            "选择信号后点击第一个采样点" if self.chart.measurement_enabled
            else ""
        )
        self.measure_label.setToolTip("")

    def _measure_at(self, x_value: float) -> None:
        if not self.chart.measurement_enabled or not self.files:
            return
        key = self.chart.focused
        if key is None or key not in self.chart.layers or not self.chart.layers[key].visible:
            self.measure_label.setText("请先在左侧选择并显示一个信号")
            return
        origin = self.files[0].first
        low, high = self.plot.viewRange()[0]
        sample = nearest_sample(
            self.db, key.storage_key(), origin + x_value,
            start=origin + low, end=origin + high,
        )
        if sample is None:
            self.measure_label.setText(f"{key.name} 当前视野内暂无采样点")
            return
        if self.measure_a is None or self.measure_b is not None or self.measure_key != key:
            self.measure_key = key
            self.measure_a = sample
            self.measure_b = None
            self.chart.set_measure_markers(sample[0] - origin, None)
            self.measure_label.setText(f"{key.name} · A {sample_clock_text(sample[0])} · 点击第二个采样点")
            self.measure_label.setToolTip(f"A = {sample[1]:.6g}，时间 {sample_clock_text(sample[0])}")
            return
        self.measure_b = sample
        self.chart.set_measure_markers(self.measure_a[0] - origin, sample[0] - origin)
        seconds = abs(sample[0] - self.measure_a[0])
        interval = f"{seconds * 1000:.3f} ms" if seconds < 1 else f"{seconds:.6f} s"
        self.measure_label.setText(f"{key.name} · Δt {interval}")
        self.measure_label.setToolTip(
            f"A {sample_clock_text(self.measure_a[0])} = {self.measure_a[1]:.6g}；"
            f"B {sample_clock_text(sample[0])} = {sample[1]:.6g}；间隔 {interval}"
        )

    def _update_cursor_values(self, x_value: float) -> None:
        self.cursor_x = x_value
        self.cursor_time_label.setText(
            cursor_clock_text(self.files[0].first + x_value) if self.files else "—"
        )
        self.cursor_absolute_label.setText(
            clock_text(self.files[0].first + x_value) if self.files else "未载入记录文件"
        )
        self.cursor_readout.setToolTip(self.cursor_absolute_label.text())
        low, high = self.plot.viewRange()[0]
        tolerance = max((high - low) * 0.05, 0.001)
        for row in range(self.curve_table.rowCount()):
            key = self.curve_table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            sample = nearest_sample(self.db, key.storage_key(), self.files[0].first + x_value) if self.files else None
            value = sample[1] if sample and abs(sample[0] - self.files[0].first - x_value) <= tolerance else None
            unit = self.curve_table.item(row, 1).data(QtCore.Qt.ItemDataRole.UserRole) or ""
            unit_suffix = f" {unit}" if unit else ""
            self.curve_table.item(row, 2).setText("—" if value is None else f"{value:.6g}{unit_suffix}")

    def _add_files(self) -> None:
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "选择 BLF 文件", "", "BLF 文件 (*.blf)")
        if not paths:
            return
        if self.worker or self.scanner or self.dbc_loader:
            QtWidgets.QMessageBox.information(self, "正在处理", "请等待当前任务结束或先停止")
            return
        merged = list(dict.fromkeys([*(item.path for item in self.files), *(Path(p).resolve() for p in paths)]))
        self.statusBar().showMessage("正在预检文件时间范围和通道…")
        self.add_files.setEnabled(False)
        self.clear_files.setEnabled(False)
        self.replay_badge.setText("预检中")
        self.scanner = ScanWorker(merged, self.scan_cache)
        self.scanner.scanned.connect(self._scan_done)
        self.scanner.failed.connect(self._scan_failed)
        self.scanner.progress.connect(self._scan_progress)
        self.scanner.finished.connect(self._scan_finished)
        self.scanner.start()
        self._update_workflow()

    def _scan_progress(self, name: str, index: int, total: int, percent: int) -> None:
        if self.scanner is None or self.scanner._stop.is_set():
            return
        self.statusBar().showMessage(f"正在预检 {index}/{total}：{name} · {percent}%（停止按钮可取消）")

    def _scan_done(self, files: list[FileInfo]) -> None:
        if self._closing or (self.scanner and self.scanner._stop.is_set()):
            return
        self._stop()
        self.files = files
        self.file_badge.setText(files[0].path.name if len(files) == 1 else f"{len(files)} 个 BLF 文件")
        self.replay_badge.setText("等待回放")
        self.playhead = files[0].first
        self.played_until = files[0].first
        self.file_table.setRowCount(len(files))
        for row, item in enumerate(files):
            for col, value in enumerate((item.path.name, clock_text(item.first), str(item.frames))):
                cell = QtWidgets.QTableWidgetItem(value)
                cell.setToolTip(str(item.path))
                self.file_table.setItem(row, col, cell)
        self._refresh_channel_table()
        self.timeline.setValue(0)
        self.chart.set_capture_origin(files[0].first)
        self.plot.setXRange(0, min(60, max(1, files[-1].last - files[0].first)), padding=0)
        self._reset_cache()
        self.statusBar().showMessage(f"已导入 {len(files)} 个文件，{sum(f.frames for f in files):,} 帧")

    def _scan_failed(self, error: str) -> None:
        if self._closing or (self.scanner and self.scanner._stop.is_set()):
            return
        self.replay_badge.setText("预检失败")
        self.statusBar().showMessage("预检失败")
        QtWidgets.QMessageBox.critical(self, "无法导入", error)

    def _scan_finished(self) -> None:
        if self._closing:
            return
        if self.scanner:
            if self.scanner._stop.is_set():
                self.replay_badge.setText("已取消预检")
            self.scanner.deleteLater()
        self.scanner = None
        self.add_files.setEnabled(True)
        self.clear_files.setEnabled(True)
        self._update_workflow()

    def _refresh_channel_table(self) -> None:
        recorded = {channel for item in self.files for channel in item.channels}
        channels = sorted(recorded | set(self.mapping) | set(self.missing_mappings) | self.unconfigured_channels)
        current = None
        if self.channel_table.currentRow() >= 0 and self.channel_table.item(self.channel_table.currentRow(), 0):
            current = self.channel_table.item(self.channel_table.currentRow(), 0).data(QtCore.Qt.ItemDataRole.UserRole)
        self.channel_table.setRowCount(len(channels))
        for row, channel in enumerate(channels):
            label = str(channel) + (" · BLF" if channel in recorded else "")
            channel_item = QtWidgets.QTableWidgetItem(label)
            channel_item.setData(QtCore.Qt.ItemDataRole.UserRole, channel)
            self.channel_table.setItem(row, 0, channel_item)
            if channel in self.mapping:
                value = str(self.mapping[channel])
            elif channel in self.missing_mappings:
                value = f"缺失：{self.missing_mappings[channel].absolute_path}"
            else:
                value = "未指定"
            cell = QtWidgets.QTableWidgetItem(self.mapping[channel].name if channel in self.mapping else value)
            cell.setToolTip(value)
            self.channel_table.setItem(row, 1, cell)
            if channel == current:
                self.channel_table.selectRow(row)

    def _selected_channel(self) -> int | None:
        row = self.channel_table.currentRow()
        if row < 0:
            return None
        item = self.channel_table.item(row, 0)
        value = item.data(QtCore.Qt.ItemDataRole.UserRole)
        return int(value) if value is not None else int(item.text().split()[0])

    def _add_channel_mapping(self) -> None:
        channel, accepted = QtWidgets.QInputDialog.getInt(self, "新增通道", "CAN 通道号", 1, 0, 65535)
        if not accepted:
            return
        if channel not in self.mapping and channel not in self.missing_mappings:
            self.unconfigured_channels.add(channel)
            self._refresh_channel_table()
        for row in range(self.channel_table.rowCount()):
            if self.channel_table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole) == channel:
                self.channel_table.selectRow(row)
                break

    def _remove_channel_mapping(self) -> None:
        channel = self._selected_channel()
        if channel is None:
            return
        self._cancel_dbc_import()
        self._stop()
        self.mapping.pop(channel, None)
        self.missing_mappings.pop(channel, None)
        self.unconfigured_channels.discard(channel)
        self.selected = {key for key in self.selected if key.channel != channel}
        self.unresolved_selected = {key: value for key, value in self.unresolved_selected.items()
                                    if key.channel != channel}
        self._reload_available_signals()
        self._refresh_channel_table()
        self._reset_cache()
        self._mark_project_dirty()

    def _assign_dbc(self) -> None:
        channel = self._selected_channel()
        if channel is None:
            QtWidgets.QMessageBox.information(self, "选择通道", "请先选择一个通道")
            return
        recent = self._recent("recent/dbcs")
        start = str(Path(recent[0]).parent) if recent else ""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择 DBC", start, "DBC 文件 (*.dbc)")
        if not path:
            return
        self._assign_dbc_path(Path(path), channel)

    def _assign_dbc_path(self, dbc_path: Path, channel: int | None = None) -> None:
        if self.worker or self.scanner:
            self.statusBar().showMessage("请先停止当前任务，再修改 DBC 映射")
            return
        if self.dbc_loader is not None:
            self.statusBar().showMessage("DBC 正在载入，请稍候…")
            return
        if channel is None:
            channel = self._selected_channel()
        if channel is None:
            QtWidgets.QMessageBox.information(self, "选择通道", "请先选择一个通道")
            return
        self.signal_timer.stop()
        if self.signal_list.count():
            self.selected, self.unresolved_selected = self._selection_from_list()
        self._dbc_generation += 1
        generation = self._dbc_generation
        loader = DbcLoadWorker(self.mapping, channel, dbc_path, self.selected, self.unresolved_selected)
        self.dbc_loader = loader
        self.signal_list.setEnabled(False)
        self.signal_search.setEnabled(False)
        self.statusBar().showMessage(f"正在载入 DBC：{dbc_path.name}…")
        loader.finished.connect(lambda: self._dbc_loaded(loader, generation))
        loader.start()
        self._update_workflow()

    def _dbc_loaded(self, loader: DbcLoadWorker, generation: int) -> None:
        if self._closing:
            return
        loader.delivered = True
        if generation != self._dbc_generation or loader.result is None:
            self.dbc_loader = None
            self.signal_list.setEnabled(True)
            self.signal_search.setEnabled(True)
            if generation == self._dbc_generation and loader.error:
                self.statusBar().showMessage("DBC 载入失败，保留原映射")
                QtWidgets.QMessageBox.critical(self, "DBC 无法读取", loader.error)
            loader.deleteLater()
            return
        self._stop()
        self.mapping, self.signals, self.selected, self.unresolved_selected = loader.result
        self.missing_mappings.pop(loader.channel, None)
        self.unconfigured_channels.discard(loader.channel)
        self._remember_recent("recent/dbcs", loader.path)
        self._reset_cache()
        self._refresh_channel_table()
        self._mark_project_dirty()
        self._sync_curves()
        def ready():
            self.dbc_loader = None
            loader.deleteLater()
            self.signal_list.setEnabled(True)
            self.signal_search.setEnabled(True)
            self.statusBar().showMessage(f"DBC 已载入，可用信号 {len(self.signals):,} 个")
        self._populate_signals(on_finished=ready)

    def _cancel_dbc_import(self) -> None:
        self._dbc_generation += 1
        self._signal_population += 1
        self._populating_signals = False
        self.signal_list.blockSignals(False)
        self.signal_list.setEnabled(True)
        self.signal_search.setEnabled(True)
        if self.dbc_loader:
            self.dbc_loader.requestInterruption()
            if getattr(self.dbc_loader, "delivered", False):
                self.dbc_loader.deleteLater()
                self.dbc_loader = None

    def _reload_available_signals(self) -> None:
        try:
            self.signals = available_signals(load_databases(self.mapping))
        except Exception:
            self.signals = []
        self.selected.intersection_update(self.signals)
        self._populate_signals()
        self._sync_curves()

    def _populate_signals(self, on_finished=None) -> None:
        self._signal_population += 1
        generation = self._signal_population
        self._populating_signals = True
        self.signal_list.blockSignals(True)
        self.signal_list.clear()
        try:
            databases = load_databases(self.mapping)
        except Exception:
            databases = {}
        seen_messages = set()
        rows = iter([(key, False) for key in self.signals] +
                    [(key, True) for key in sorted(self.unresolved_selected, key=lambda item: item.label())])
        def batch():
            if generation != self._signal_population:
                return
            started = time.monotonic()
            query = self.signal_search.text().lower().strip()
            for key, unresolved in rows:
                label = ("[未解析] " if unresolved else "") + key.label()
                item = QtWidgets.QListWidgetItem(label)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
                message_id = (key.channel, key.frame_id, key.extended)
                if message_id not in seen_messages:
                    seen_messages.add(message_id)
                    message_name = ""
                    if key.channel in databases:
                        try:
                            message_name = databases[key.channel].get_message_by_frame_id(key.frame_id).name
                        except KeyError:
                            pass
                    item.setData(QtCore.Qt.ItemDataRole.UserRole + 2,
                                 f"CAN {key.channel}  ·  0x{key.frame_id:X}"
                                 + (f"  ·  {message_name}" if message_name else ""))
                item.setData(QtCore.Qt.ItemDataRole.UserRole + 3, signal_unit(key, databases))
                if unresolved:
                    item.setData(QtCore.Qt.ItemDataRole.UserRole + 1, True)
                item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(QtCore.Qt.CheckState.Checked if unresolved or key in self.selected
                                   else QtCore.Qt.CheckState.Unchecked)
                self.signal_list.addItem(item)
                item.setHidden(query not in label.lower())
                if on_finished and time.monotonic() - started >= 0.008:
                    QtCore.QTimer.singleShot(0, self, batch)
                    return
            self._populating_signals = False
            self.signal_list.blockSignals(False)
            self.config_signal_count.setText(f"可用信号 {len(self.signals)}  ·  {len(self.selected) + len(self.unresolved_selected)} 个已选")
            self._update_config_signal_count()
            if on_finished:
                on_finished()
        batch()

    def _signal_selection_changed(self, _item: QtWidgets.QListWidgetItem) -> None:
        self._update_config_signal_count()
        self._mark_project_dirty()
        self.signal_timer.start()

    def _selection_from_list(self) -> tuple[set[SignalKey], dict[SignalKey, SignalReference]]:
        if self._populating_signals:
            return set(self.selected), dict(self.unresolved_selected)
        chosen: set[SignalKey] = set()
        unresolved: dict[SignalKey, SignalReference] = {}
        for row in range(self.signal_list.count()):
            item = self.signal_list.item(row)
            if item.checkState() != QtCore.Qt.CheckState.Checked:
                continue
            key = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if item.data(QtCore.Qt.ItemDataRole.UserRole + 1):
                if key in self.unresolved_selected:
                    unresolved[key] = self.unresolved_selected[key]
            elif key in self.signals:
                chosen.add(key)
        for key in chosen:
            unresolved.pop(key, None)
        return chosen, unresolved

    def _update_config_signal_count(self) -> None:
        chosen = sum(self.signal_list.item(row).checkState() == QtCore.Qt.CheckState.Checked
                     for row in range(self.signal_list.count()))
        self.config_signal_count.setText(f"可用信号 {len(self.signals)}  ·  {chosen} 个已选")
        names = [self.signal_list.item(row).data(QtCore.Qt.ItemDataRole.UserRole).name
                 for row in range(self.signal_list.count())
                 if self.signal_list.item(row).checkState() == QtCore.Qt.CheckState.Checked]
        preview = " · ".join(names[:3])
        if len(names) > 3:
            preview += f"  等 {len(names)} 个信号"
        self.selection_summary.setText(f"当前已选  {preview}" if names else "当前未选择信号")
        self.selection_summary.setToolTip("\n".join(names))

    def _filter_signals(self, query: str) -> None:
        query = query.lower().strip()
        for row in range(self.signal_list.count()):
            item = self.signal_list.item(row)
            item.setHidden(query not in item.text().lower())

    def _apply_signals(self) -> None:
        if self._closing:
            return
        chosen, unresolved = self._selection_from_list()
        changed = chosen != self.selected or unresolved != self.unresolved_selected
        new = chosen - self.cached
        self.selected = chosen
        if unresolved != self.unresolved_selected:
            self.unresolved_selected = unresolved
            self._populate_signals()
        if changed:
            self._mark_project_dirty()
        self._sync_curves()
        if new and self.files and self.played_until > self.files[0].first:
            if self.worker:
                self.statusBar().showMessage("播放中已更新选择；播放结束后可补画新信号")
                return
            self._set_signal_notice(f"正在补画 {len(new)} 个新信号…")
            self._launch(new, backfill=True)
        else:
            self._refresh_plot()
            if not self.worker:
                self._update_signal_notice()

    def _sync_curves(self) -> None:
        if self.measure_key is not None and self.measure_key not in self.selected:
            self._clear_measurement()
        visible = {key: layer.visible for key, layer in self.chart.layers.items()}
        for key in list(self.curves):
            if key not in self.selected:
                self.chart.remove_signal(key)
                self.curves.pop(key)
                self.curve_data.pop(key, None)
                self._plot_versions.pop(key, None)
        try:
            databases = load_databases(self.mapping)
        except Exception:
            databases = {}
        for key in sorted(self.selected - self.curves.keys(), key=lambda item: item.label()):
            self.curves[key] = self.chart.add_signal(key, key.label(), signal_unit(key, databases))
            if self.chart.y_locked:
                fingerprint = signal_definition_fingerprint(key, databases) or "unknown"
                saved = self._saved_y_ranges().get(f"{key.storage_key()}|{fingerprint}")
                if isinstance(saved, list) and len(saved) == 2:
                    self.chart.set_y_range(key, float(saved[0]), float(saved[1]))
        self.curve_table.blockSignals(True)
        self.curve_table.setRowCount(0)
        for row, key in enumerate(sorted(self.selected, key=lambda item: item.label())):
            self.curve_table.insertRow(row)
            color = signal_color(key)
            show = QtWidgets.QTableWidgetItem()
            show.setData(QtCore.Qt.ItemDataRole.UserRole, key)
            show.setFlags(show.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            is_visible = visible.get(key, row < 3)
            show.setCheckState(QtCore.Qt.CheckState.Checked if is_visible else QtCore.Qt.CheckState.Unchecked)
            self.curve_table.setItem(row, 0, show)
            unit = signal_unit(key, databases) or ""
            name = QtWidgets.QTableWidgetItem(key.name)
            name.setToolTip(key.label())
            name.setData(QtCore.Qt.ItemDataRole.UserRole, unit)
            name.setForeground(QtGui.QColor(color))
            self.curve_table.setItem(row, 1, name)
            self.curve_table.setItem(row, 2, QtWidgets.QTableWidgetItem("—"))
            self.chart.set_visible(key, is_visible)
        self.curve_table.blockSignals(False)
        self._update_curve_summary()
        if self.cursor_x is not None:
            self._update_cursor_values(self.cursor_x)

    def _start(self) -> None:
        if self.scanner or self.dbc_loader:
            self.statusBar().showMessage("正在准备数据，请等待完成后开始回放")
            return
        if not self.files:
            QtWidgets.QMessageBox.information(self, "缺少文件", "请先导入 BLF 文件")
            return
        if self.worker:
            if self.pause_button.text() == "继续":
                self.worker.set_paused(False)
                self.pause_button.setText("暂停")
                self.replay_badge.setText("回放中")
            return
        self._apply_signals_without_backfill()
        if self.playhead >= self.files[-1].last:
            self.playhead = self.files[0].first
        self._launch(self.selected, start_at=self.playhead)

    def _apply_signals_without_backfill(self, mark_dirty: bool = True) -> None:
        self.signal_timer.stop()
        selected, unresolved = self._selection_from_list()
        changed = selected != self.selected or unresolved != self.unresolved_selected
        self.selected, self.unresolved_selected = selected, unresolved
        self._sync_curves()
        if mark_dirty and changed:
            self._mark_project_dirty()

    def _launch(self, signals: set[SignalKey], start_at: float | None = None, backfill: bool = False) -> None:
        self._replay_error = None
        self.worker = ReplayWorker(self.files, self.mapping.copy(), signals.copy(), self.cache_path,
                                   start_at, backfill, poll_updates=True)
        worker = self.worker
        worker.advanced.connect(lambda data: self._advanced(worker, data))
        worker.failed.connect(lambda error: self._worker_failed(worker, error))
        worker.completed.connect(lambda complete: self._completed(worker, complete, signals, backfill))
        worker.finished.connect(lambda: self._worker_finished(worker))
        worker.start()
        self.replay_badge.setText("补画中" if backfill else "回放中")
        self.statusBar().showMessage("正在补画信号…" if backfill else "正在回放…")
        self._update_workflow()

    def _poll_replay(self) -> None:
        if self._closing:
            return
        if self.worker:
            data = self.worker.take_update()
            if data is not None:
                self._advanced(self.worker, data)
        self._update_workflow()

    def _advanced(self, worker: ReplayWorker, data: tuple) -> None:
        if self._closing or worker is not self.worker:
            return
        file_index, processed, total, timestamp, raw_rows = data
        if not worker.backfill:
            self.playhead = timestamp
            self.played_until = max(self.played_until, timestamp)
            self.progress.setValue(int(processed / max(1, total) * 1000))
            self.timeline.blockSignals(True)
            duration = self.files[-1].last - self.files[0].first
            self.timeline.setValue(int((timestamp - self.files[0].first) / max(0.001, duration) * 10000))
            self.timeline.blockSignals(False)
            self.time_label.setText(clock_text(timestamp))
            self._append_raw(raw_rows)
            if self.follow.isChecked():
                x = timestamp - self.files[0].first
                window = self.follow_window.value()
                self.plot.setXRange(max(0, x - window), max(1.0, x), padding=0)
            self.statusBar().showMessage(f"{self.files[file_index].path.name} · {processed:,}/{total:,} 帧")

    def _append_raw(self, rows: list[tuple]) -> None:
        # The worker supplies a rolling snapshot, so reuse cells instead of
        # allocating and shifting thousands of table rows on every update.
        rows = rows[-1000:]
        self.raw_table.setUpdatesEnabled(False)
        try:
            self.raw_table.setRowCount(len(rows))
            for row, (timestamp, channel, frame_id, kind, data) in enumerate(rows):
                for col, value in enumerate((clock_text(timestamp), str(channel), f"0x{frame_id:X}", kind, data)):
                    cell = self.raw_table.item(row, col)
                    if cell is None:
                        self.raw_table.setItem(row, col, QtWidgets.QTableWidgetItem(value))
                    elif cell.text() != value:
                        cell.setText(value)
            if rows:
                self.raw_table.scrollToBottom()
        finally:
            self.raw_table.setUpdatesEnabled(True)

    def _completed(self, worker: ReplayWorker, complete: bool, signals: set[SignalKey], backfill: bool) -> None:
        if self._closing or worker is not self.worker:
            return
        self._poll_replay()
        if complete:
            if backfill or worker.start_at is None or worker.start_at <= self.files[0].first:
                self.cached |= signals
            self.statusBar().showMessage("补画完成" if backfill else "回放完成")
            self.replay_badge.setText("补画完成" if backfill else "回放完成")
        else:
            self.statusBar().showMessage(f"回放失败：{self._replay_error}" if self._replay_error else "任务已停止")
            self.replay_badge.setText("回放失败" if self._replay_error else "已停止")
        self.pause_button.setText("暂停")
        self._refresh_plot()
        if complete and not backfill and self.follow.isChecked():
            self._fit_xy()
        self._update_signal_notice()

    def _worker_finished(self, worker: ReplayWorker) -> None:
        if not self._closing and worker is self.worker:
            self.worker = None
            missing = self.selected - self.cached
            if worker.succeeded and missing and self.files and self.played_until > self.files[0].first:
                self._set_signal_notice(f"正在补画 {len(missing)} 个新信号…")
                self._launch(missing, backfill=True)
            self._update_workflow()

    def _toggle_signal_notice(self, expanded: bool) -> None:
        self.notice_scroll.setVisible(expanded and bool(self.signal_notice.text()))
        self.notice_toggle.setArrowType(
            QtCore.Qt.ArrowType.DownArrow if expanded else QtCore.Qt.ArrowType.RightArrow
        )
        self.notice_toggle.setText("收起提示详情" if expanded else "提示详情")

    def _set_signal_notice(self, text: str) -> None:
        self.signal_notice.setText(text)
        self.notice_toggle.setVisible(bool(text))
        if not text:
            self.notice_toggle.setChecked(False)
        self._toggle_signal_notice(self.notice_toggle.isChecked())

    def _update_signal_notice(self) -> None:
        if not self.files or not self.selected:
            self._set_signal_notice("")
            return
        empty = [key for key in sorted(self.selected, key=lambda item: item.label())
                 if self.db.execute("SELECT 1 FROM samples WHERE signal = ? LIMIT 1",
                                    (key.storage_key(),)).fetchone() is None]
        if not empty:
            self._set_signal_notice("")
            return
        try:
            databases = load_databases(self.mapping)
        except Exception:
            databases = {}
        details = [f"{key.name}：{missing_signal_reason(key, self.files, databases)}" for key in empty]
        self._set_signal_notice("无波形数据：" + "；\n".join(details))

    def _worker_failed(self, worker: ReplayWorker, error: str) -> None:
        if not self._closing and worker is self.worker:
            self._replay_error = error
            self.replay_badge.setText("回放失败")
            QtWidgets.QMessageBox.critical(self, "回放失败", error)

    def _pause(self) -> None:
        if not self.worker:
            return
        paused = self.pause_button.text() == "暂停"
        self.worker.set_paused(paused)
        self.pause_button.setText("继续" if paused else "暂停")
        self.replay_badge.setText("已暂停" if paused else "回放中")
        self.statusBar().showMessage("已暂停" if paused else "正在回放…")

    def _stop_tasks(self) -> None:
        if self.scanner:
            self.scanner.stop()
        if self.dbc_loader and not self._populating_signals:
            self._cancel_dbc_import()
        self._stop()
        self.statusBar().showMessage("已停止")

    def _stop(self) -> None:
        if self.worker:
            worker = self.worker
            worker.stop()
            worker.wait()
            self._poll_replay()
            self.worker = None
            self.pause_button.setText("暂停")
            self.replay_badge.setText("已停止")
            self.statusBar().showMessage("已停止")

    def _seek(self) -> None:
        if not self.files:
            return
        duration = self.files[-1].last - self.files[0].first
        target = self.files[0].first + duration * self.timeline.value() / 10000
        was_running = self.worker is not None and not self.worker.backfill
        if self.worker:
            self._stop()
        self.playhead = target
        self.time_label.setText(clock_text(target))
        self.follow.setChecked(False)
        self.plot.setXRange(max(0, target - self.files[0].first - 30), target - self.files[0].first + 30)
        if was_running:
            self._launch(self.selected, start_at=target)

    def _refresh_plot(self, *, all_visible: bool = False) -> None:
        if self._closing or not self.files or not self.curves:
            return
        origin = self.files[0].first
        low, high = self.plot.viewRange()[0]
        version = (origin, low, high, self.db.execute("PRAGMA data_version").fetchone()[0],
                   self.db.total_changes)
        visible_keys = [key for key in self.curves if self.chart.layers[key].visible]
        if not visible_keys:
            return
        batch_size = len(visible_keys) if all_visible else min(20, len(visible_keys))
        offset = getattr(self, "_plot_refresh_offset", 0) % len(visible_keys)
        keys = [visible_keys[(offset + index) % len(visible_keys)] for index in range(batch_size)]
        self._plot_refresh_offset = (offset + batch_size) % len(visible_keys)
        for key in keys:
            if not all_visible and self._plot_versions.get(key) == version:
                continue
            x, y, sample_x, sample_y = plot_points_with_samples(
                self.db, key.storage_key(), origin + max(0, low), origin + high
            )
            relative_x = [value - origin for value in x]
            self.curve_data[key] = (relative_x, y)
            self.chart.set_data(key, relative_x, y, [value - origin for value in sample_x], sample_y)
            self._plot_versions[key] = version
        if self.cursor_x is not None:
            self._update_cursor_values(self.cursor_x)

    def _reset_cache(self) -> None:
        self._clear_measurement()
        self.db.close()
        self.cache_dir.cleanup()
        self.cache_dir = tempfile.TemporaryDirectory(prefix="canflow-")
        self.cache_path = Path(self.cache_dir.name) / "samples.sqlite"
        self.db = connect(self.cache_path)
        self.cached.clear()
        self.curve_data.clear()
        self._plot_versions.clear()
        self.cursor_x = None
        self.cursor_time_label.setText("—")
        self.cursor_absolute_label.setText("移动到波形查看游标时间")
        self.cursor_readout.setToolTip(self.cursor_absolute_label.text())
        self._set_signal_notice("")
        self.raw_table.setRowCount(0)
        self.progress.setValue(0)
        for key, curve in self.curves.items():
            curve.setData([], [])
            self.chart.layers[key].samples.setData([], [])

    def _clear_recordings(self) -> None:
        if self.scanner:
            self.scanner.stop()
        self._stop()
        self.files.clear()
        self.file_badge.setText("未导入 BLF")
        self.replay_badge.setText("等待导入")
        self.file_table.setRowCount(0)
        self.playhead = 0.0
        self.played_until = 0.0
        self.timeline.setValue(0)
        self.time_label.setText("—")
        self.chart.set_capture_origin(None)
        self._reset_cache()
        self._refresh_channel_table()
        self.statusBar().showMessage("请选择 BLF 文件")
        self._update_workflow()

    def _clear(self) -> None:
        self._cancel_dbc_import()
        self.signal_timer.stop()
        self._clear_recordings()
        self.mapping.clear()
        self.missing_mappings.clear()
        self.unconfigured_channels.clear()
        self.signals.clear()
        self.selected.clear()
        self.unresolved_selected.clear()
        self.channel_table.setRowCount(0)
        self.signal_list.clear()
        self.chart.clear()
        self.curves.clear()
        self.curve_data.clear()
        self.curve_table.setRowCount(0)
        self._update_curve_summary()
        self.statusBar().showMessage("请选择 BLF 文件")

    def shutdown(self) -> None:
        """Drain this session's workers before releasing its SQLite cache."""
        if self._closing:
            return
        self._closing = True
        for editor in (self.files_dialog, self.mapping_dialog, self.signals_dialog,
                       self.display_panel, self.playback_details):
            editor.close()
        self._cancel_dbc_import()
        if self.dbc_loader:
            self.dbc_loader.wait()
        self._stop()
        self.plot_timer.stop()
        self.progress_timer.stop()
        self.signal_timer.stop()
        if self.scanner:
            self.scanner.stop()
            self.scanner.wait()
        self.db.close()
        self.cache_dir.cleanup()

    def closeEvent(self, event) -> None:
        if not self._closing and not self._maybe_save_project():
            event.ignore()
            return
        self.shutdown()
        super().closeEvent(event)


def main() -> None:
    application = QtWidgets.QApplication(sys.argv)
    windows_font = Path(r"C:\Windows\Fonts\msyh.ttc")
    if windows_font.exists():
        QtGui.QFontDatabase.addApplicationFont(str(windows_font))
        application.setFont(QtGui.QFont("Microsoft YaHei UI", 9))
    application.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(application.exec())


if __name__ == "__main__":
    main()
