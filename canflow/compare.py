from __future__ import annotations

import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import can
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from .core import available_signals, load_databases, prepare_decoders, signal_unit, usable_frame
from .store import connect, configure_overview, nearest_sample, plot_points_with_samples, write_samples
from .workers import DbcLoadWorker, ScanCache, ScanWorker


def clock(timestamp):
    return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S.%f')


class CompareWorker(QtCore.QThread):
    progress = QtCore.Signal(str)
    failed = QtCore.Signal(str)
    completed = QtCore.Signal(bool)

    def __init__(self, sides, paths):
        super().__init__()
        self.sides, self.paths = sides, paths
        self.cancelled = threading.Event()

    def stop(self):
        self.cancelled.set()

    def run(self):
        try:
            for index, (side, path) in enumerate(zip(self.sides, self.paths)):
                db = connect(path)
                try:
                    configure_overview(db, side['start'], side['end'])
                    decoders = prepare_decoders(load_databases(side['mapping']), side['selected'])
                    batch = []
                    processed, last_emit = 0, 0.0
                    with can.BLFReader(side['file'].path) as reader:
                        for message in reader:
                            if self.cancelled.is_set():
                                self.completed.emit(False)
                                return
                            if not usable_frame(message):
                                continue
                            processed += 1
                            timestamp = float(message.timestamp)
                            if side['start'] <= timestamp <= side['end']:
                                channel = int(message.channel) if message.channel is not None else -1
                                decoder = decoders.get((channel, message.arbitration_id, message.is_extended_id))
                                if decoder:
                                    batch.extend((key, timestamp, value) for key, value in decoder(message.data))
                            if len(batch) >= 16000:
                                write_samples(db, batch)
                                batch.clear()
                            now = time.monotonic()
                            if processed % 256 == 0 and now - last_emit >= .2:
                                self.progress.emit(f"正在解码 {'AB'[index]}：{min(100, processed * 100 // max(1, side['file'].frames))}%")
                                last_emit = now
                    write_samples(db, batch)
                finally:
                    db.close()
            self.completed.emit(not self.cancelled.is_set())
        except Exception as exc:
            self.failed.emit(str(exc))
            self.completed.emit(False)


class CompareSide(QtWidgets.QGroupBox):
    changed = QtCore.Signal()
    mapping_changed = QtCore.Signal()

    def __init__(self, title, parent=None):
        super().__init__(f'记录 {title}', parent)
        self.info = None
        self.mapping = {}
        self.databases = {}
        self.scanner = None
        self.loader = None
        self.cache = ScanCache()
        layout = QtWidgets.QGridLayout(self)
        self.file_button = QtWidgets.QPushButton('选择 BLF…')
        self.file_button.clicked.connect(self.choose_file)
        self.file_label = QtWidgets.QLabel('尚未选择文件')
        self.file_label.setWordWrap(True)
        self.range_label = QtWidgets.QLabel('采集范围：—')
        layout.addWidget(self.file_button, 0, 0)
        layout.addWidget(self.file_label, 0, 1, 1, 3)
        layout.addWidget(self.range_label, 1, 0, 1, 4)
        layout.addWidget(QtWidgets.QLabel('起点 / 终点（本地时间）'), 2, 0)
        self.start = QtWidgets.QLineEdit()
        self.end = QtWidgets.QLineEdit()
        self.start.setPlaceholderText('年-月-日 时:分:秒.微秒')
        self.end.setPlaceholderText('年-月-日 时:分:秒.微秒')
        layout.addWidget(self.start, 2, 1)
        layout.addWidget(self.end, 2, 2, 1, 2)
        layout.addWidget(QtWidgets.QLabel('通道'), 3, 0)
        self.channel = QtWidgets.QSpinBox()
        self.channel.setRange(0, 65535)
        self.channel.setValue(1)
        layout.addWidget(self.channel, 3, 1)
        self.dbc_button = QtWidgets.QPushButton('为通道选择 DBC…')
        self.dbc_button.clicked.connect(self.choose_dbc)
        layout.addWidget(self.dbc_button, 3, 2, 1, 2)
        self.mapping_label = QtWidgets.QLabel('DBC 映射：—')
        self.mapping_label.setWordWrap(True)
        layout.addWidget(self.mapping_label, 4, 0, 1, 4)
        layout.addWidget(QtWidgets.QLabel('信号'), 5, 0)
        self.signal = QtWidgets.QComboBox()
        self.signal.setMinimumContentsLength(25)
        layout.addWidget(self.signal, 5, 1, 1, 3)
        self.status = QtWidgets.QLabel('')
        layout.addWidget(self.status, 6, 0, 1, 3)
        self.stop_button = QtWidgets.QPushButton('取消预检')
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.cancel_scan)
        layout.addWidget(self.stop_button, 6, 3)
        self.start.textChanged.connect(self.changed)
        self.end.textChanged.connect(self.changed)

    def choose_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '选择 BLF', '', 'BLF 文件 (*.blf)')
        if not path:
            return
        self.file_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status.setText('正在预检…')
        self.scanner = ScanWorker([Path(path)], self.cache)
        self.scanner.progress.connect(lambda _name, _i, _n, p: self.status.setText(f'正在预检：{p}%'))
        self.scanner.failed.connect(lambda message: self.status.setText(f'预检失败：{message}'))
        self.scanner.scanned.connect(self._scanned)
        self.scanner.finished.connect(self._scan_finished)
        self.scanner.start()

    def _scanned(self, files):
        self.info = files[0]
        self.file_label.setText(str(self.info.path))
        self.range_label.setText(f'采集范围：{clock(self.info.first)} — {clock(self.info.last)}  · 通道 {", ".join(map(str, self.info.channels))}')
        self.start.setText(clock(self.info.first))
        self.end.setText(clock(self.info.last))
        self.status.setText('预检完成')
        self.changed.emit()

    def _scan_finished(self):
        if self.scanner and self.scanner._stop.is_set():
            self.status.setText('已取消预检。')
        self.scanner = None
        self.file_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def cancel_scan(self):
        if self.scanner:
            self.scanner.stop()
            self.status.setText('正在取消预检…')

    def choose_dbc(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '选择 DBC', '', 'DBC 文件 (*.dbc)')
        if not path:
            return
        self.dbc_button.setEnabled(False)
        self.status.setText('正在解析 DBC…')
        self.loader = DbcLoadWorker(self.mapping, self.channel.value(), Path(path), set(), {})
        self.loader.finished.connect(self._dbc_finished)
        self.loader.start()

    def _dbc_finished(self):
        loader = self.loader
        self.loader = None
        self.dbc_button.setEnabled(True)
        if loader.error:
            self.status.setText(f'DBC 解析失败：{loader.error}')
            return
        if not loader.result:
            return
        self.mapping, signals, _, _ = loader.result
        self.databases = load_databases(self.mapping)
        self.signal.clear()
        for key in signals:
            unit = signal_unit(key, self.databases) or ''
            self.signal.addItem(f'{key.label()}  [{unit}]', key)
        self.mapping_label.setText('DBC 映射：' + '；'.join(f'CH{ch}: {path.name}' for ch, path in sorted(self.mapping.items())))
        self.status.setText(f'已加载 {len(signals)} 个信号')
        self.mapping_changed.emit()
        self.changed.emit()

    def stop(self):
        if self.scanner:
            self.scanner.stop()
            self.scanner.wait()
        if self.loader:
            self.loader.requestInterruption()
            self.loader.wait()

    def snapshot(self, selected):
        if not self.info:
            raise ValueError('请先选择并完成预检 BLF 文件')
        try:
            def parse(value):
                try:
                    return float(value)
                except ValueError:
                    return datetime.fromisoformat(value).timestamp()
            start, end = parse(self.start.text()), parse(self.end.text())
        except (ValueError, OverflowError, OSError):
            raise ValueError('请输入本地时间（年-月-日 时:分:秒.微秒）或 Unix 秒数') from None
        # The displayed clock has microsecond precision; preserve boundary samples
        # when a BLF timestamp rounds by less than one microsecond.
        if abs(start - self.info.first) < 1e-6:
            start = self.info.first
        if abs(end - self.info.last) < 1e-6:
            end = self.info.last
        if not self.info.first <= start < end <= self.info.last:
            raise ValueError('片段须在文件采集范围内，且终点必须大于起点')
        return dict(file=self.info, mapping=dict(self.mapping), start=start, end=end, selected=selected)


class CompareDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('双 BLF 片段对比')
        self.resize(1450, 900)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.temp = tempfile.TemporaryDirectory(prefix='canflow-compare-')
        self.paths = [Path(self.temp.name) / f'{side}.sqlite' for side in 'ab']
        self.connections = []
        self.worker = None
        self.pairs = []
        self.sides_snapshot = None
        self.closing = False
        self.plots = []
        layout = QtWidgets.QVBoxLayout(self)
        side_layout = QtWidgets.QHBoxLayout()
        self.sides = [CompareSide('A'), CompareSide('B')]
        for side in self.sides:
            side_layout.addWidget(side)
            side.changed.connect(self.invalidate)
            side.mapping_changed.connect(self.clear_pairs)
        layout.addLayout(side_layout)
        controls = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QPushButton('添加信号配对')
        self.add_button.clicked.connect(self.add_pair)
        self.remove_button = QtWidgets.QPushButton('移除选中配对')
        self.remove_button.clicked.connect(self.remove_pair)
        self.run_button = QtWidgets.QPushButton('开始对比')
        self.run_button.clicked.connect(self.start_compare)
        self.cancel_button = QtWidgets.QPushButton('取消处理')
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        for widget in (self.add_button, self.remove_button, self.run_button, self.cancel_button):
            controls.addWidget(widget)
        controls.addStretch()
        layout.addLayout(controls)
        self.pair_list = QtWidgets.QListWidget()
        self.pair_list.setMaximumHeight(100)
        layout.addWidget(self.pair_list)
        self.status = QtWidgets.QLabel('选择两个文件、配置 DBC、添加信号配对后开始对比。')
        layout.addWidget(self.status)
        self.graphics = pg.GraphicsLayoutWidget()
        self.graphics.setBackground('#101e30')
        layout.addWidget(self.graphics, 1)
        self.readout = QtWidgets.QLabel('移动鼠标查看两侧原始采样值和时间')
        self.readout.setWordWrap(True)
        layout.addWidget(self.readout)
        self.graphics.scene().sigMouseMoved.connect(self.mouse_moved)
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(80)
        self.refresh_timer.timeout.connect(self.refresh)

    def invalidate(self):
        if self.sides_snapshot is not None:
            self.status.setText('配置已更改，请重新开始对比。')
            self.sides_snapshot = None
            self.clear_results()
            self.readout.setText('配置已更改')
    def clear_pairs(self):
        # DBC changes can invalidate keys: require a fresh explicit pairing.
        self.pairs.clear()
        self.pair_list.clear()

    def add_pair(self):
        keys = tuple(side.signal.currentData() for side in self.sides)
        if any(key is None for key in keys):
            self.status.setText('请先为两侧配置 DBC 并选择信号。')
            return
        if keys in self.pairs:
            self.status.setText('该信号配对已存在。')
            return
        self.pairs.append(keys)
        units = [signal_unit(key, side.databases) or '' for key, side in zip(keys, self.sides)]
        warning = '  ⚠ 单位不同，使用独立纵轴' if units[0] != units[1] else ''
        self.pair_list.addItem(f'A: {keys[0].label()} [{units[0]}]  ↔  B: {keys[1].label()} [{units[1]}]{warning}')
        if self.sides_snapshot:
            self.sides_snapshot = None
            self.clear_results()
            self.status.setText('配对已更改，请重新开始对比。')

    def remove_pair(self):
        row = self.pair_list.currentRow()
        if row >= 0:
            self.pairs.pop(row)
            self.pair_list.takeItem(row)
            self.sides_snapshot = None
            self.clear_results()
            self.status.setText('配对已更改，请重新开始对比。')

    def start_compare(self):
        if any(side.scanner or side.loader for side in self.sides):
            self.status.setText('请等待文件或 DBC 处理完成。')
            return
        if not self.pairs:
            self.status.setText('请至少添加一组信号配对。')
            return
        try:
            snapshots = [side.snapshot({pair[i] for pair in self.pairs}) for i, side in enumerate(self.sides)]
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.clear_results()
        self.sides_snapshot = snapshots
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        for side in self.sides:
            side.setEnabled(False)
        self.pair_list.setEnabled(False)
        self.add_button.setEnabled(False)
        self.remove_button.setEnabled(False)
        self.worker = CompareWorker(snapshots, self.paths)
        self.worker.progress.connect(self.status.setText)
        self.worker.failed.connect(lambda msg: self.status.setText(f'对比失败：{msg}'))
        self.worker.completed.connect(self._completed)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    def cancel(self):
        if self.worker:
            self.worker.stop()
            self.status.setText('正在取消…')

    def _completed(self, success):
        if self.closing:
            return
        if not success:
            if self.worker and self.worker.cancelled.is_set():
                self.status.setText('已取消处理。')
            self.sides_snapshot = None
            return
        self.connections = [connect(path) for path in self.paths]
        self.build_plots()
        missing = []
        for i, side in enumerate(self.sides_snapshot):
            for key in side['selected']:
                if nearest_sample(self.connections[i], key.storage_key(), side['start'], start=side['start'], end=side['end']) is None:
                    missing.append(f"{'AB'[i]}: {key.label()}")
        self.status.setText('对比完成。' + (' 无可解码数据：' + '；'.join(missing) if missing else ''))

    def _worker_finished(self):
        self.worker = None
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        for side in self.sides:
            side.setEnabled(True)
        self.pair_list.setEnabled(True)
        self.add_button.setEnabled(True)
        self.remove_button.setEnabled(True)

    def clear_results(self):
        self.refresh_timer.stop()
        for _, right, _, _, _ in self.plots:
            if right is not None:
                self.graphics.scene().removeItem(right)
        self.graphics.clear()
        self.plots.clear()
        for db in self.connections:
            db.close()
        self.connections.clear()
        for path in self.paths:
            for suffix in ('', '-wal', '-shm'):
                Path(str(path) + suffix).unlink(missing_ok=True)

    def build_plots(self):
        duration = max(s['end'] - s['start'] for s in self.sides_snapshot)
        master = None
        for row, pair in enumerate(self.pairs):
            plot = self.graphics.addPlot(row=row, col=0)
            plot.disableAutoRange(axis=pg.ViewBox.XAxis)
            plot.showGrid(x=True, y=True, alpha=.2)
            plot.setLabel('bottom', '相对片段起点', units='s')
            plot.addLegend()
            units = [signal_unit(key, side.databases) or '' for key, side in zip(pair, self.sides)]
            plot.setLabel('left', f'A [{units[0]}]', color='#38bdf8')
            plot.setTitle(f'A: {pair[0].label()} [{units[0]}]    |    B: {pair[1].label()} [{units[1]}]' + ('   ⚠ 单位不同' if units[0] != units[1] else ''))
            a = plot.plot(pen=pg.mkPen('#38bdf8', width=2), name='A')
            a_samples = pg.ScatterPlotItem(
                [], [], symbol='o', size=5, pen=None, brush=pg.mkBrush('#38bdf8')
            )
            plot.addItem(a_samples)
            right = None
            if units[0] != units[1]:
                plot.showAxis('right')
                plot.setLabel('right', f'B [{units[1]}]', color='#fb7185')
                right = pg.ViewBox(enableMenu=False)
                right.setMouseEnabled(x=False, y=False)
                plot.scene().addItem(right)
                plot.getAxis('right').linkToView(right)
                right.setXLink(plot.vb)
                b = pg.PlotDataItem(pen=pg.mkPen('#fb7185', width=2), name='B')
                right.addItem(b)
                b_samples = pg.ScatterPlotItem(
                    [], [], symbol='o', size=5, pen=None, brush=pg.mkBrush('#fb7185')
                )
                right.addItem(b_samples)
                plot.legend.addItem(b, 'B')
                def sync(*_args, plot=plot, right=right):
                    right.setGeometry(plot.vb.sceneBoundingRect())
                    right.linkedViewChanged(plot.vb, right.XAxis)
                plot.vb.sigResized.connect(sync)
                sync()
            else:
                b = plot.plot(pen=pg.mkPen('#fb7185', width=2), name='B')
                b_samples = pg.ScatterPlotItem(
                    [], [], symbol='o', size=5, pen=None, brush=pg.mkBrush('#fb7185')
                )
                plot.addItem(b_samples)
            line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('#ffffff'))
            line.hide()
            plot.addItem(line, ignoreBounds=True)
            if master is None:
                master = plot
                plot.setXRange(0, duration, padding=0)
            else:
                plot.setXLink(master)
            plot.vb.sigXRangeChanged.connect(lambda *_: self.refresh_timer.start())
            self.plots.append((plot, right, (a, b), line, (a_samples, b_samples)))
        self.refresh()

    def refresh(self):
        if not self.sides_snapshot or not self.connections:
            return
        for pair, (plot, right, curves, _, sample_markers) in zip(self.pairs, self.plots):
            low, high = plot.vb.viewRange()[0]
            for i, (key, curve) in enumerate(zip(pair, curves)):
                side = self.sides_snapshot[i]
                start = max(side['start'], side['start'] + low)
                end = min(side['end'], side['start'] + high)
                x, y, sample_x, sample_y = plot_points_with_samples(
                    self.connections[i], key.storage_key(), start, end
                )
                curve.setData([v - side['start'] for v in x], y)
                sample_markers[i].setData(
                    [v - side['start'] for v in sample_x], sample_y
                )
            if right:
                right.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)

    def mouse_moved(self, position):
        if not self.sides_snapshot or not self.plots:
            return
        source = next((p for p, _, _, _, _ in self.plots
                       if p.vb.sceneBoundingRect().contains(position)), None)
        if source is None:
            return
        x = float(source.vb.mapSceneToView(position).x())
        low, high = source.vb.viewRange()[0]
        tolerance = max((high - low) * .05, .001)
        lines = []
        for pair, (_, _, _, line, _) in zip(self.pairs, self.plots):
            line.setValue(x)
            line.show()
            values = []
            for i, key in enumerate(pair):
                side = self.sides_snapshot[i]
                target = side['start'] + x
                sample = None
                if side['start'] <= target <= side['end']:
                    sample = nearest_sample(self.connections[i], key.storage_key(), target, start=side['start'], end=side['end'])
                if sample and abs(sample[0] - target) <= tolerance:
                    unit = signal_unit(key, self.sides[i].databases) or ''
                    values.append(f"{'AB'[i]} {sample[1]:.6g} {unit} · {clock(sample[0])}")
                else:
                    values.append(f"{'AB'[i]} —")
            lines.append(f'{pair[0].name} ↔ {pair[1].name}: ' + '    '.join(values))
        self.readout.setText(f'相对时间 {x:.6f} s    |    ' + ' ；  '.join(lines))

    def closeEvent(self, event):
        self.closing = True
        if self.worker:
            self.worker.stop()
            self.worker.wait()
        for side in self.sides:
            side.stop()
        self.clear_results()
        self.temp.cleanup()
        super().closeEvent(event)
