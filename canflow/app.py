from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from .core import FileInfo, SignalKey, available_signals, load_databases, missing_signal_reason
from .store import connect, plot_points
from .workers import ReplayWorker, ScanWorker


def clock_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CANFlow · BLF 波形回放")
        self.resize(1450, 900)
        self.files: list[FileInfo] = []
        self.mapping: dict[int, Path] = {}
        self.signals: list[SignalKey] = []
        self.playhead = 0.0
        self.played_until = 0.0
        self.selected: set[SignalKey] = set()
        self.cached: set[SignalKey] = set()
        self.worker: ReplayWorker | None = None
        self.scanner: ScanWorker | None = None
        self.cache_dir = tempfile.TemporaryDirectory(prefix="canflow-")
        self.cache_path = Path(self.cache_dir.name) / "samples.sqlite"
        self.db = connect(self.cache_path)
        self.curves: dict[SignalKey, pg.PlotDataItem] = {}
        self.signal_timer = QtCore.QTimer(self)
        self.signal_timer.setSingleShot(True)
        self.signal_timer.setInterval(150)
        self.signal_timer.timeout.connect(self._apply_signals)
        self._build_ui()
        self.plot_timer = QtCore.QTimer(self)
        self.plot_timer.setInterval(500)
        self.plot_timer.timeout.connect(self._refresh_plot)
        self.plot_timer.start()

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        toolbar = QtWidgets.QHBoxLayout()
        root.addLayout(toolbar)
        self.add_files = QtWidgets.QPushButton("添加 BLF 文件")
        self.add_files.clicked.connect(self._add_files)
        toolbar.addWidget(self.add_files)
        self.clear_files = QtWidgets.QPushButton("清空")
        self.clear_files.clicked.connect(self._clear)
        toolbar.addWidget(self.clear_files)
        toolbar.addStretch()
        self.start_button = QtWidgets.QPushButton("开始 / 继续")
        self.start_button.clicked.connect(self._start)
        toolbar.addWidget(self.start_button)
        self.pause_button = QtWidgets.QPushButton("暂停")
        self.pause_button.clicked.connect(self._pause)
        toolbar.addWidget(self.pause_button)
        self.stop_button = QtWidgets.QPushButton("停止")
        self.stop_button.clicked.connect(self._stop)
        toolbar.addWidget(self.stop_button)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)
        side = QtWidgets.QWidget()
        side.setMinimumWidth(330)
        side.setMaximumWidth(450)
        side_layout = QtWidgets.QVBoxLayout(side)
        splitter.addWidget(side)
        side_layout.addWidget(QtWidgets.QLabel("播放顺序（按首帧时间）"))
        self.file_table = QtWidgets.QTableWidget(0, 3)
        self.file_table.setHorizontalHeaderLabels(["文件", "首帧时间", "帧数"])
        self.file_table.horizontalHeader().setStretchLastSection(True)
        self.file_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        side_layout.addWidget(self.file_table, 2)
        side_layout.addWidget(QtWidgets.QLabel("通道 DBC（选中行后指定）"))
        self.channel_table = QtWidgets.QTableWidget(0, 2)
        self.channel_table.setHorizontalHeaderLabels(["通道", "DBC"])
        self.channel_table.horizontalHeader().setStretchLastSection(True)
        self.channel_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.channel_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        side_layout.addWidget(self.channel_table, 1)
        set_dbc = QtWidgets.QPushButton("为选中通道指定 DBC")
        set_dbc.clicked.connect(self._assign_dbc)
        side_layout.addWidget(set_dbc)
        self.signal_search = QtWidgets.QLineEdit()
        self.signal_search.setPlaceholderText("搜索信号 / 报文 ID")
        self.signal_search.textChanged.connect(self._filter_signals)
        side_layout.addWidget(self.signal_search)
        self.signal_list = QtWidgets.QListWidget()
        self.signal_list.itemChanged.connect(lambda _item: self.signal_timer.start())
        side_layout.addWidget(self.signal_list, 2)
        apply_signals = QtWidgets.QPushButton("应用所选信号 / 补画历史")
        apply_signals.clicked.connect(self._apply_signals)
        side_layout.addWidget(apply_signals)
        self.signal_notice = QtWidgets.QLabel()
        self.signal_notice.setWordWrap(True)
        self.signal_notice.setStyleSheet("color: #a35400;")
        side_layout.addWidget(self.signal_notice)

        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 3)
        self.plot = pg.PlotWidget(background="#111827")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.addLegend()
        self.plot.setLabel("bottom", "相对首帧时间", units="s")
        self.plot.setLabel("left", "DBC 信号值")
        right_layout.addWidget(self.plot, 4)
        row = QtWidgets.QHBoxLayout()
        right_layout.addLayout(row)
        self.follow = QtWidgets.QCheckBox("跟随播放（60 秒窗口）")
        self.follow.setChecked(True)
        row.addWidget(self.follow)
        row.addWidget(QtWidgets.QLabel("时间轴"))
        self.timeline = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.timeline.setRange(0, 10000)
        self.timeline.sliderReleased.connect(self._seek)
        row.addWidget(self.timeline, 1)
        self.time_label = QtWidgets.QLabel("—")
        row.addWidget(self.time_label)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        right_layout.addWidget(self.progress)
        right_layout.addWidget(QtWidgets.QLabel("原始 CAN / CAN FD 报文（最近 1000 条）"))
        self.raw_table = QtWidgets.QTableWidget(0, 5)
        self.raw_table.setHorizontalHeaderLabels(["时间", "通道", "ID", "类型", "数据"])
        self.raw_table.horizontalHeader().setStretchLastSection(True)
        self.raw_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        right_layout.addWidget(self.raw_table, 2)
        self.statusBar().showMessage("请选择 BLF 文件")

    def _add_files(self) -> None:
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "选择 BLF 文件", "", "BLF 文件 (*.blf)")
        if not paths:
            return
        if self.worker or self.scanner:
            QtWidgets.QMessageBox.information(self, "正在处理", "请等待当前任务结束或先停止")
            return
        merged = list(dict.fromkeys([*(item.path for item in self.files), *(Path(p).resolve() for p in paths)]))
        self.statusBar().showMessage("正在预检文件时间范围和通道…")
        self.add_files.setEnabled(False)
        self.clear_files.setEnabled(False)
        self.scanner = ScanWorker(merged)
        self.scanner.scanned.connect(self._scan_done)
        self.scanner.failed.connect(self._scan_failed)
        self.scanner.finished.connect(self._scan_finished)
        self.scanner.start()

    def _scan_done(self, files: list[FileInfo]) -> None:
        self._stop()
        self.files = files
        self.playhead = files[0].first
        self.played_until = files[0].first
        self.file_table.setRowCount(len(files))
        for row, item in enumerate(files):
            for col, value in enumerate((item.path.name, clock_text(item.first), str(item.frames))):
                cell = QtWidgets.QTableWidgetItem(value)
                cell.setToolTip(str(item.path))
                self.file_table.setItem(row, col, cell)
        channels = sorted({channel for item in files for channel in item.channels})
        self.channel_table.setRowCount(len(channels))
        for row, channel in enumerate(channels):
            self.channel_table.setItem(row, 0, QtWidgets.QTableWidgetItem(str(channel)))
            self.channel_table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(self.mapping.get(channel, "未指定"))))
        self.mapping = {channel: path for channel, path in self.mapping.items() if channel in channels}
        self.timeline.setValue(0)
        self.plot.setXRange(0, min(60, max(1, files[-1].last - files[0].first)), padding=0)
        self._reset_cache()
        self.statusBar().showMessage(f"已导入 {len(files)} 个文件，{sum(f.frames for f in files):,} 帧")

    def _scan_failed(self, error: str) -> None:
        self.statusBar().showMessage("预检失败")
        QtWidgets.QMessageBox.critical(self, "无法导入", error)

    def _scan_finished(self) -> None:
        self.scanner = None
        self.add_files.setEnabled(True)
        self.clear_files.setEnabled(True)

    def _assign_dbc(self) -> None:
        row = self.channel_table.currentRow()
        if row < 0:
            QtWidgets.QMessageBox.information(self, "选择通道", "请先选择一个通道")
            return
        channel = int(self.channel_table.item(row, 0).text())
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择 DBC", "", "DBC 文件 (*.dbc)")
        if not path:
            return
        try:
            test_mapping = {**self.mapping, channel: Path(path)}
            databases = load_databases(test_mapping)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "DBC 无法读取", str(exc))
            return
        self._stop()
        self.mapping = test_mapping
        self.channel_table.item(row, 1).setText(path)
        self.signals = available_signals(databases)
        self.selected.intersection_update(self.signals)
        self._reset_cache()
        self._populate_signals()

    def _populate_signals(self) -> None:
        self.signal_list.blockSignals(True)
        self.signal_list.clear()
        for key in self.signals:
            item = QtWidgets.QListWidgetItem(key.label())
            item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked if key in self.selected else QtCore.Qt.CheckState.Unchecked)
            self.signal_list.addItem(item)
        self.signal_list.blockSignals(False)
        self._filter_signals(self.signal_search.text())

    def _filter_signals(self, query: str) -> None:
        query = query.lower().strip()
        for row in range(self.signal_list.count()):
            item = self.signal_list.item(row)
            item.setHidden(query not in item.text().lower())

    def _apply_signals(self) -> None:
        chosen = {self.signal_list.item(row).data(QtCore.Qt.ItemDataRole.UserRole)
                  for row in range(self.signal_list.count())
                  if self.signal_list.item(row).checkState() == QtCore.Qt.CheckState.Checked}
        new = chosen - self.cached
        self.selected = chosen
        self._sync_curves()
        if new and self.files and self.played_until > self.files[0].first:
            if self.worker:
                self.statusBar().showMessage("播放中已更新选择；播放结束后可补画新信号")
                return
            self.signal_notice.setText(f"正在补画 {len(new)} 个新信号…")
            self._launch(new, backfill=True)
        else:
            self._refresh_plot()
            if not self.worker:
                self._update_signal_notice()

    def _sync_curves(self) -> None:
        for key in list(self.curves):
            if key not in self.selected:
                self.plot.removeItem(self.curves.pop(key))
        colors = ["#38bdf8", "#fb7185", "#a3e635", "#fbbf24", "#c084fc", "#2dd4bf"]
        for key in sorted(self.selected - self.curves.keys(), key=lambda item: item.label()):
            color = colors[len(self.curves) % len(colors)]
            self.curves[key] = self.plot.plot([], [], pen=pg.mkPen(color, width=1), name=key.label(),
                                              autoDownsample=True, clipToView=True)

    def _start(self) -> None:
        if not self.files:
            QtWidgets.QMessageBox.information(self, "缺少文件", "请先导入 BLF 文件")
            return
        if self.worker:
            if self.pause_button.text() == "继续":
                self.worker.set_paused(False)
                self.pause_button.setText("暂停")
            return
        self._apply_signals_without_backfill()
        if self.playhead >= self.files[-1].last:
            self.playhead = self.files[0].first
        self._launch(self.selected, start_at=self.playhead)

    def _apply_signals_without_backfill(self) -> None:
        self.selected = {self.signal_list.item(row).data(QtCore.Qt.ItemDataRole.UserRole)
                         for row in range(self.signal_list.count())
                         if self.signal_list.item(row).checkState() == QtCore.Qt.CheckState.Checked}
        self._sync_curves()

    def _launch(self, signals: set[SignalKey], start_at: float | None = None, backfill: bool = False) -> None:
        self.worker = ReplayWorker(self.files, self.mapping.copy(), signals.copy(), self.cache_path, start_at, backfill)
        worker = self.worker
        worker.advanced.connect(lambda data: self._advanced(worker, data))
        worker.failed.connect(lambda error: self._worker_failed(worker, error))
        worker.completed.connect(lambda complete: self._completed(worker, complete, signals, backfill))
        worker.finished.connect(lambda: self._worker_finished(worker))
        worker.start()
        self.statusBar().showMessage("正在补画信号…" if backfill else "正在回放…")

    def _advanced(self, worker: ReplayWorker, data: tuple) -> None:
        if worker is not self.worker:
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
                self.plot.setXRange(max(0, x - 60), max(60, x), padding=0)
            self.statusBar().showMessage(f"{self.files[file_index].path.name} · {processed:,}/{total:,} 帧")

    def _append_raw(self, rows: list[tuple]) -> None:
        for timestamp, channel, frame_id, kind, data in rows:
            row = self.raw_table.rowCount()
            self.raw_table.insertRow(row)
            for col, value in enumerate((clock_text(timestamp), str(channel), f"0x{frame_id:X}", kind, data)):
                self.raw_table.setItem(row, col, QtWidgets.QTableWidgetItem(value))
        while self.raw_table.rowCount() > 1000:
            self.raw_table.removeRow(0)
        if rows:
            self.raw_table.scrollToBottom()

    def _completed(self, worker: ReplayWorker, complete: bool, signals: set[SignalKey], backfill: bool) -> None:
        if worker is not self.worker:
            return
        if complete:
            if backfill or worker.start_at is None or worker.start_at <= self.files[0].first:
                self.cached |= signals
            self.statusBar().showMessage("补画完成" if backfill else "回放完成")
        else:
            self.statusBar().showMessage("任务已停止")
        self.pause_button.setText("暂停")
        self._refresh_plot()
        self._update_signal_notice()

    def _worker_finished(self, worker: ReplayWorker) -> None:
        if worker is self.worker:
            self.worker = None
            missing = self.selected - self.cached
            if missing and self.files and self.played_until > self.files[0].first:
                self.signal_notice.setText(f"正在补画 {len(missing)} 个新信号…")
                self._launch(missing, backfill=True)

    def _update_signal_notice(self) -> None:
        if not self.files or not self.selected:
            self.signal_notice.clear()
            return
        empty = [key for key in sorted(self.selected, key=lambda item: item.label())
                 if self.db.execute("SELECT 1 FROM samples WHERE signal = ? LIMIT 1",
                                    (key.storage_key(),)).fetchone() is None]
        if not empty:
            self.signal_notice.clear()
            return
        try:
            databases = load_databases(self.mapping)
        except Exception:
            databases = {}
        details = [f"{key.name}：{missing_signal_reason(key, self.files, databases)}" for key in empty]
        self.signal_notice.setText("无波形数据：" + "；".join(details))

    def _worker_failed(self, worker: ReplayWorker, error: str) -> None:
        if worker is self.worker:
            QtWidgets.QMessageBox.critical(self, "回放失败", error)

    def _pause(self) -> None:
        if not self.worker:
            return
        paused = self.pause_button.text() == "暂停"
        self.worker.set_paused(paused)
        self.pause_button.setText("继续" if paused else "暂停")
        self.statusBar().showMessage("已暂停" if paused else "正在回放…")

    def _stop(self) -> None:
        if self.worker:
            worker = self.worker
            worker.stop()
            worker.wait()
            self.worker = None
            self.pause_button.setText("暂停")
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

    def _refresh_plot(self) -> None:
        if not self.files or not self.curves:
            return
        origin = self.files[0].first
        low, high = self.plot.viewRange()[0]
        for key, curve in self.curves.items():
            x, y = plot_points(self.db, key.storage_key(), origin + max(0, low), origin + high)
            curve.setData([value - origin for value in x], y)

    def _reset_cache(self) -> None:
        self.db.close()
        self.cache_dir.cleanup()
        self.cache_dir = tempfile.TemporaryDirectory(prefix="canflow-")
        self.cache_path = Path(self.cache_dir.name) / "samples.sqlite"
        self.db = connect(self.cache_path)
        self.cached.clear()
        self.signal_notice.clear()
        self.raw_table.setRowCount(0)
        self.progress.setValue(0)
        for curve in self.curves.values():
            curve.setData([], [])

    def _clear(self) -> None:
        self._stop()
        self.files.clear()
        self.mapping.clear()
        self.signals.clear()
        self.selected.clear()
        self.file_table.setRowCount(0)
        self.channel_table.setRowCount(0)
        self.signal_list.clear()
        for curve in self.curves.values():
            self.plot.removeItem(curve)
        self.curves.clear()
        self._reset_cache()
        self.statusBar().showMessage("请选择 BLF 文件")

    def closeEvent(self, event) -> None:
        self._stop()
        if self.scanner:
            self.scanner.stop()
            self.scanner.wait()
        self.db.close()
        self.cache_dir.cleanup()
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
