from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from .chart import MultiSignalChart, signal_color
from .core import (
    FileInfo, SignalKey, available_signals, load_databases, missing_signal_reason,
    signal_definition_fingerprint, signal_unit,
)
from .persistence import (
    DbcReference, SignalGroup, SignalGroupStore, SignalReference, export_signal_group, import_signal_group,
    load_project, make_signal_references, resolve_dbc_reference, save_project,
)
from .store import connect, nearest_sample, plot_points
from .workers import ReplayWorker, ScanWorker


def clock_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def cursor_clock_text(timestamp: float) -> str:
    moment = datetime.fromtimestamp(timestamp)
    return f"{moment:%H点%M分%S秒}{moment.microsecond // 1000}毫秒"


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CANFlow · BLF 波形回放")
        self.resize(1450, 900)
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
        self.scanner: ScanWorker | None = None
        self.cache_dir = tempfile.TemporaryDirectory(prefix="canflow-")
        self.cache_path = Path(self.cache_dir.name) / "samples.sqlite"
        self.db = connect(self.cache_path)
        self.curves: dict[SignalKey, pg.PlotDataItem] = {}
        self.curve_data: dict[SignalKey, tuple[list[float], list[float]]] = {}
        self.cursor_x: float | None = None
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

    def _build_ui(self) -> None:
        file_menu = self.menuBar().addMenu("项目")
        for text, shortcut, callback in (
            ("新建", "Ctrl+N", self._new_project),
            ("打开…", "Ctrl+O", self._open_project),
            ("保存", "Ctrl+S", self._save_project),
            ("另存为…", "Ctrl+Shift+S", self._save_project_as),
        ):
            action = file_menu.addAction(text)
            action.setShortcut(shortcut)
            action.triggered.connect(callback)
        self.recent_project_menu = file_menu.addMenu("最近项目")
        file_menu.aboutToShow.connect(self._populate_recent_projects)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(20, 12, 20, 10)
        root.setSpacing(10)
        header = QtWidgets.QHBoxLayout()
        root.addLayout(header)
        brand = QtWidgets.QLabel("CANFlow")
        brand.setObjectName("brand")
        header.addWidget(brand)
        subtitle = QtWidgets.QLabel("BLF 波形回放")
        subtitle.setObjectName("mutedLabel")
        header.addWidget(subtitle)
        header.addStretch()
        self.file_badge = QtWidgets.QLabel("未导入 BLF")
        self.file_badge.setObjectName("fileBadge")
        header.addWidget(self.file_badge)
        self.replay_badge = QtWidgets.QLabel("等待导入")
        self.replay_badge.setObjectName("replayBadge")
        header.addWidget(self.replay_badge)
        self.pages = QtWidgets.QTabWidget()
        root.addWidget(self.pages, 1)
        wave_page = QtWidgets.QWidget()
        wave_layout = QtWidgets.QVBoxLayout(wave_page)
        wave_layout.setContentsMargins(0, 8, 0, 0)
        config_page = QtWidgets.QWidget()
        config_layout = QtWidgets.QVBoxLayout(config_page)
        config_layout.setContentsMargins(0, 8, 0, 0)
        self.pages.addTab(wave_page, "波形")
        self.pages.addTab(config_page, "文件与信号配置")
        config_toolbar = QtWidgets.QHBoxLayout()
        config_layout.addLayout(config_toolbar)
        config_title = QtWidgets.QLabel("文件与信号配置")
        config_title.setObjectName("pageHeading")
        config_toolbar.addWidget(config_title)
        config_subtitle = QtWidgets.QLabel("导入 BLF、映射 DBC，然后选择需要查看的信号")
        config_subtitle.setObjectName("mutedLabel")
        config_toolbar.addWidget(config_subtitle)
        config_toolbar.addStretch()
        toolbar = QtWidgets.QHBoxLayout()
        self.fit_button = QtWidgets.QPushButton("自动适配 X/Y")
        self.fit_button.clicked.connect(self._fit_xy)
        toolbar.addWidget(self.fit_button)
        self.time_mode_combo = QtWidgets.QComboBox()
        self.time_mode_combo.addItem("相对时间", "relative")
        self.time_mode_combo.addItem("原始采集时间", "capture")
        saved_mode = self.settings.value("chart/time_mode", "relative", type=str)
        self.time_mode_combo.setCurrentIndex(1 if saved_mode == "capture" else 0)
        toolbar.addWidget(self.time_mode_combo)
        self.add_files = QtWidgets.QPushButton("添加 BLF 文件")
        self.add_files.clicked.connect(self._add_files)
        self.add_files.setText("+ 添加 BLF 文件")
        self.add_files.setObjectName("primaryButton")
        config_toolbar.addWidget(self.add_files)
        self.clear_files = QtWidgets.QPushButton("清空")
        self.clear_files.clicked.connect(self._clear_recordings)
        config_toolbar.addWidget(self.clear_files)
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

        config_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.config_splitter = config_splitter
        config_layout.addWidget(config_splitter, 1)
        recording_panel = QtWidgets.QWidget()
        recording_panel.setObjectName("recordingPanel")
        side_layout = QtWidgets.QVBoxLayout(recording_panel)
        config_splitter.addWidget(recording_panel)
        signal_panel = QtWidgets.QWidget()
        signal_panel.setObjectName("signalPanel")
        signal_layout = QtWidgets.QVBoxLayout(signal_panel)
        config_splitter.addWidget(signal_panel)
        config_splitter.setSizes([650, 750])
        config_splitter.setStretchFactor(0, 1)
        config_splitter.setStretchFactor(1, 1)
        file_heading = QtWidgets.QLabel("01  记录文件")
        file_heading.setObjectName("sectionHeading")
        side_layout.addWidget(file_heading)
        file_hint = QtWidgets.QLabel("按首帧时间排序，逐个文件连续回放")
        file_hint.setObjectName("mutedLabel")
        side_layout.addWidget(file_hint)
        self.file_table = QtWidgets.QTableWidget(0, 3)
        self.file_table.setHorizontalHeaderLabels(["文件", "首帧时间", "帧数"])
        self.file_table.horizontalHeader().setStretchLastSection(True)
        self.file_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        side_layout.addWidget(self.file_table, 2)
        map_heading = QtWidgets.QLabel("02  DBC 通道映射")
        map_heading.setObjectName("sectionHeading")
        side_layout.addWidget(map_heading)
        map_hint = QtWidgets.QLabel("DBC 独立于 BLF，仅用于解码信号")
        map_hint.setObjectName("mutedLabel")
        side_layout.addWidget(map_hint)
        self.channel_table = QtWidgets.QTableWidget(0, 2)
        self.channel_table.setHorizontalHeaderLabels(["通道", "DBC"])
        self.channel_table.horizontalHeader().setStretchLastSection(True)
        self.channel_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.channel_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        side_layout.addWidget(self.channel_table, 1)
        channel_buttons = QtWidgets.QHBoxLayout()
        add_channel = QtWidgets.QPushButton("新增通道")
        add_channel.clicked.connect(self._add_channel_mapping)
        channel_buttons.addWidget(add_channel)
        set_dbc = QtWidgets.QPushButton("指定 DBC")
        set_dbc.clicked.connect(self._assign_dbc)
        channel_buttons.addWidget(set_dbc)
        recent_dbc = QtWidgets.QToolButton()
        recent_dbc.setText("最近 DBC")
        recent_dbc.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.recent_dbc_menu = QtWidgets.QMenu(recent_dbc)
        self.recent_dbc_menu.aboutToShow.connect(self._populate_recent_dbcs)
        recent_dbc.setMenu(self.recent_dbc_menu)
        channel_buttons.addWidget(recent_dbc)
        remove_channel = QtWidgets.QPushButton("删除映射")
        remove_channel.clicked.connect(self._remove_channel_mapping)
        channel_buttons.addWidget(remove_channel)
        side_layout.addLayout(channel_buttons)

        group_row = QtWidgets.QHBoxLayout()
        self.group_combo = QtWidgets.QComboBox()
        group_row.addWidget(self.group_combo, 1)
        apply_group = QtWidgets.QPushButton("替换")
        apply_group.clicked.connect(lambda: self._apply_signal_group(False))
        group_row.addWidget(apply_group)
        append_group = QtWidgets.QPushButton("追加")
        append_group.clicked.connect(lambda: self._apply_signal_group(True))
        group_row.addWidget(append_group)
        signals_heading = QtWidgets.QLabel("03  信号与信号组")
        signals_heading.setObjectName("sectionHeading")
        signal_layout.addWidget(signals_heading)
        signals_hint = QtWidgets.QLabel("勾选信号后应用，即可在波形页查看游标值")
        signals_hint.setObjectName("mutedLabel")
        signal_layout.addWidget(signals_hint)
        group_manage = QtWidgets.QHBoxLayout()
        for text, callback in (("保存组", self._save_signal_group), ("删除组", self._delete_signal_group),
                               ("导入", self._import_signal_group), ("导出", self._export_signal_group)):
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(callback)
            group_manage.addWidget(button)
        self._populate_group_combo()
        self.signal_search = QtWidgets.QLineEdit()
        self.signal_search.setPlaceholderText("搜索信号 / 报文 ID")
        self.signal_search.textChanged.connect(self._filter_signals)
        signal_layout.addWidget(self.signal_search)
        self.config_signal_count = QtWidgets.QLabel("0 个已选")
        self.config_signal_count.setObjectName("mutedLabel")
        signal_layout.addWidget(self.config_signal_count)
        self.signal_list = QtWidgets.QListWidget()
        self.signal_list.itemChanged.connect(self._signal_selection_changed)
        signal_layout.addWidget(self.signal_list, 1)
        group_heading = QtWidgets.QLabel("信号组")
        group_heading.setObjectName("mutedLabel")
        signal_layout.addWidget(group_heading)
        signal_layout.addLayout(group_row)
        signal_layout.addLayout(group_manage)
        apply_signals = QtWidgets.QPushButton("应用所选信号 / 补画历史")
        apply_signals.setObjectName("primaryButton")
        apply_signals.clicked.connect(self._apply_signals)
        signal_layout.addWidget(apply_signals)
        self.notice_toggle = QtWidgets.QToolButton()
        self.notice_toggle.setText("提示详情")
        self.notice_toggle.setCheckable(True)
        self.notice_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.notice_toggle.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        self.notice_toggle.hide()
        signal_layout.addWidget(self.notice_toggle)
        self.signal_notice = QtWidgets.QLabel()
        self.signal_notice.setWordWrap(True)
        self.signal_notice.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.signal_notice.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.signal_notice.setStyleSheet("color: #a35400;")
        self.notice_scroll = QtWidgets.QScrollArea()
        self.notice_scroll.setWidgetResizable(True)
        self.notice_scroll.setFixedHeight(120)
        self.notice_scroll.setWidget(self.signal_notice)
        self.notice_scroll.hide()
        self.notice_toggle.toggled.connect(self._toggle_signal_notice)
        signal_layout.addWidget(self.notice_scroll)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.wave_splitter = splitter
        wave_layout.addWidget(splitter, 1)
        inspector = QtWidgets.QWidget()
        self.sidebar = inspector
        inspector.setObjectName("inspectorPanel")
        inspector.setMinimumWidth(350)
        inspector.setMaximumWidth(480)
        inspector_layout = QtWidgets.QVBoxLayout(inspector)
        inspector_layout.setContentsMargins(16, 14, 16, 12)
        inspector_layout.setSpacing(7)
        splitter.addWidget(inspector)
        inspector_heading = QtWidgets.QLabel("信号值")
        inspector_heading.setObjectName("sectionHeading")
        inspector_layout.addWidget(inspector_heading)
        signal_heading = QtWidgets.QHBoxLayout()
        inspector_layout.addLayout(signal_heading)
        signal_heading.addStretch()
        self.signal_count_label = QtWidgets.QLabel("0 个信号")
        self.signal_count_label.setObjectName("mutedLabel")
        signal_heading.addWidget(self.signal_count_label)
        self.curve_search = QtWidgets.QLineEdit()
        self.curve_search.setPlaceholderText("搜索信号 / 报文 ID")
        self.curve_search.setClearButtonEnabled(True)
        self.curve_search.textChanged.connect(self._filter_curve_rows)
        inspector_layout.addWidget(self.curve_search)

        right = QtWidgets.QWidget()
        right.setObjectName("chartPanel")
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(16, 14, 16, 12)
        right_layout.setSpacing(7)
        splitter.addWidget(right)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([390, 1150])
        splitter.setStretchFactor(1, 4)
        cursor_header = QtWidgets.QHBoxLayout()
        right_layout.addLayout(cursor_header)
        heading = QtWidgets.QLabel("信号波形")
        heading.setObjectName("sectionHeading")
        cursor_header.addWidget(heading)
        self.chart_summary_label = QtWidgets.QLabel("0 条已选 · 0 条显示")
        self.chart_summary_label.setObjectName("mutedLabel")
        cursor_header.addWidget(self.chart_summary_label)
        cursor_header.addStretch()
        self.cursor_readout = QtWidgets.QWidget()
        self.cursor_readout.setObjectName("cursorReadout")
        self.cursor_readout.setToolTip("移动到波形查看游标时间")
        cursor_readout_layout = QtWidgets.QHBoxLayout(self.cursor_readout)
        cursor_readout_layout.setContentsMargins(8, 3, 8, 3)
        cursor_readout_layout.setSpacing(5)
        cursor_readout_layout.addWidget(QtWidgets.QLabel("游标"))
        self.cursor_time_label = QtWidgets.QLabel("—")
        self.cursor_time_label.setObjectName("cursorTime")
        cursor_readout_layout.addWidget(self.cursor_time_label)
        self.cursor_absolute_label = QtWidgets.QLabel("移动到波形查看游标时间")
        self.cursor_absolute_label.hide()
        cursor_readout_layout.addWidget(self.cursor_absolute_label)
        cursor_header.addWidget(self.cursor_readout)
        cursor_header.addSpacing(8)
        cursor_header.addLayout(toolbar)
        self.chart = MultiSignalChart()
        self.plot = self.chart.widget
        self.chart.cursor_moved.connect(self._update_cursor_values)
        self.chart.set_line_width(self.settings.value("chart/line_width", 1.4, type=float))
        self.chart.set_grid(self.settings.value("chart/grid", True, type=bool))
        self.chart.set_theme(self.settings.value("chart/theme", "深色", type=str))
        self.chart.set_time_mode(self.time_mode_combo.currentData())
        self.time_mode_combo.currentIndexChanged.connect(self._set_time_mode)
        right_layout.addWidget(self.chart, 1)
        self.display_panel = QtWidgets.QWidget()
        chart_controls = QtWidgets.QGridLayout(self.display_panel)
        chart_controls.setContentsMargins(0, 6, 0, 6)
        chart_controls.setHorizontalSpacing(10)
        chart_controls.setVerticalSpacing(8)
        hint = QtWidgets.QLabel("Ctrl + 滚轮：同步缩放纵轴")
        hint.setStyleSheet("color: #52647b;")
        chart_controls.addWidget(hint, 0, 0, 1, 3)
        auto_y = QtWidgets.QPushButton("自动适配 Y")
        auto_y.clicked.connect(self._reset_auto_y)
        chart_controls.addWidget(auto_y, 1, 0, 1, 2)
        self.lock_y = QtWidgets.QCheckBox("锁定全部 Y 轴")
        self.lock_y.setChecked(self.settings.value("chart/y_locked", False, type=bool))
        self.chart.y_locked = self.lock_y.isChecked()
        self.lock_y.toggled.connect(self._set_y_locked)
        chart_controls.addWidget(self.lock_y, 1, 2)
        chart_controls.addWidget(QtWidgets.QLabel("线宽"), 2, 0)
        self.line_width = QtWidgets.QDoubleSpinBox()
        self.line_width.setRange(0.5, 5.0)
        self.line_width.setSingleStep(0.25)
        self.line_width.setValue(self.settings.value("chart/line_width", 1.4, type=float))
        self.line_width.valueChanged.connect(self._set_line_width)
        chart_controls.addWidget(self.line_width, 2, 1)
        self.grid_toggle = QtWidgets.QCheckBox("网格")
        self.grid_toggle.setChecked(self.settings.value("chart/grid", True, type=bool))
        self.grid_toggle.toggled.connect(self._set_grid)
        chart_controls.addWidget(self.grid_toggle, 2, 2)
        self.theme_combo = QtWidgets.QComboBox()
        self.theme_combo.addItems(["深色", "浅色"])
        self.theme_combo.setCurrentText(self.settings.value("chart/theme", "深色", type=str))
        self.theme_combo.currentTextChanged.connect(self._set_theme)
        chart_controls.addWidget(QtWidgets.QLabel("主题"), 3, 0)
        chart_controls.addWidget(self.theme_combo, 3, 1, 1, 2)
        self.curve_table = QtWidgets.QTableWidget(0, 3)
        self.curve_table.setHorizontalHeaderLabels(["显示", "信号", "当前值"])
        self.curve_table.horizontalHeader().setStretchLastSection(True)
        self.curve_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.curve_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.curve_table.itemChanged.connect(self._curve_visibility_changed)
        self.curve_table.currentCellChanged.connect(self._curve_focus_changed)
        # Let the table give space to the controls below it in a short window.
        self.curve_table.setMinimumHeight(96)
        self.curve_table.verticalHeader().hide()
        self.curve_table.verticalHeader().setDefaultSectionSize(35)
        self.curve_table.setColumnWidth(0, 45)
        self.curve_table.setColumnWidth(1, 170)
        self.curve_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.curve_table.setColumnWidth(2, 110)
        self.curve_table.setAlternatingRowColors(True)
        inspector_layout.addWidget(self.curve_table, 1)
        self.curve_footer = QtWidgets.QLabel("点击信号聚焦曲线 · 勾选显示")
        self.curve_footer.setObjectName("mutedLabel")
        inspector_layout.addWidget(self.curve_footer)
        self.display_toggle = QtWidgets.QToolButton()
        self.display_toggle.setText("显示与回放  ▸")
        self.display_toggle.setCheckable(True)
        self.display_toggle.toggled.connect(self._set_display_settings_visible)
        inspector_layout.addWidget(self.display_toggle)
        inspector_layout.addWidget(self.display_panel)
        self._set_display_settings_visible(False)
        self.details_toggle = QtWidgets.QToolButton()
        self.details_toggle.setCheckable(True)
        details_visible = self.settings.value("layout/playback_details_visible", False, type=bool)
        self.details_toggle.setChecked(details_visible)
        self.details_toggle.toggled.connect(self._set_playback_details_visible)
        self.display_panel.layout().addWidget(self.details_toggle, 4, 0, 1, 3)

        self.playback_details = QtWidgets.QWidget()
        details_layout = QtWidgets.QVBoxLayout(self.playback_details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        details_layout.addLayout(row)
        self.follow = QtWidgets.QCheckBox("跟随播放")
        self.follow.setChecked(self.settings.value("chart/follow", True, type=bool))
        self.follow.toggled.connect(lambda value: self.settings.setValue("chart/follow", value))
        row.addWidget(self.follow)
        self.follow_window = QtWidgets.QSpinBox()
        self.follow_window.setRange(5, 3600)
        self.follow_window.setSuffix(" 秒")
        self.follow_window.setValue(self.settings.value("chart/follow_window", 60, type=int))
        self.follow_window.valueChanged.connect(lambda value: self.settings.setValue("chart/follow_window", value))
        row.addWidget(self.follow_window)
        row.addWidget(QtWidgets.QLabel("时间轴"))
        self.timeline = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.timeline.setRange(0, 10000)
        self.timeline.sliderReleased.connect(self._seek)
        row.addWidget(self.timeline, 1)
        self.time_label = QtWidgets.QLabel("—")
        row.addWidget(self.time_label)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        details_layout.addWidget(self.progress)
        details_layout.addWidget(QtWidgets.QLabel("原始 CAN / CAN FD 报文（最近 1000 条）"))
        self.raw_table = QtWidgets.QTableWidget(0, 5)
        self.raw_table.setHorizontalHeaderLabels(["时间", "通道", "ID", "类型", "数据"])
        self.raw_table.horizontalHeader().setStretchLastSection(True)
        self.raw_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        details_layout.addWidget(self.raw_table, 1)
        right_layout.addWidget(self.playback_details, 1)
        self._set_playback_details_visible(details_visible, persist=False)
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #101a2b; color: #e8effa; }
            QLabel { background: transparent; }
            QWidget#inspectorPanel, QWidget#chartPanel, QWidget#recordingPanel,
            QWidget#signalPanel { background: #172337; border: 1px solid #2a3951;
                border-radius: 12px; }
            QWidget#cursorReadout { background: #223854; border-radius: 7px; }
            QMenuBar, QMenu, QStatusBar { background: #111d2f; color: #dce7f5; }
            QMenuBar::item:selected, QMenu::item:selected { background: #28405d; }
            QTabWidget::pane { border: 0; background: #101a2b; }
            QTabBar::tab { background: #111d2f; color: #93a4bd;
                padding: 10px 18px; border: 0; }
            QTabBar::tab:selected { color: #e8effa; border-bottom: 3px solid #49c7f5; }
            QPushButton, QToolButton, QComboBox, QDoubleSpinBox, QSpinBox {
                background: #1d2c42; color: #e8effa; border: 1px solid #33455e;
                border-radius: 7px; padding: 5px 9px; min-height: 22px;
            }
            QPushButton:hover, QToolButton:hover { border-color: #49c7f5; }
            QPushButton:pressed, QToolButton:checked { background: #244b69; }
            QPushButton#primaryButton { background: #235273; border-color: #367fa8; }
            QPushButton#primaryButton:hover { background: #28658d; }
            QLineEdit, QListWidget, QTableWidget, QScrollArea {
                background: #142033; color: #e8effa; border: 1px solid #2a3951;
                border-radius: 6px; selection-background-color: #294763;
            }
            QLineEdit { padding: 6px 9px; }
            QHeaderView::section { background: #22344d; border: 0;
                border-bottom: 1px solid #2a3951; padding: 5px; color: #93a4bd; }
            QTableWidget { alternate-background-color: #19283d; gridline-color: #26374d; }
            QTableWidget::item:selected, QListWidget::item:selected {
                background: #294763; color: #e8effa; }
            QScrollBar:vertical { background: #172337; width: 9px; }
            QScrollBar::handle:vertical { background: #405b79; border-radius: 4px; }
            QLabel#brand { font-size: 21px; font-weight: 700; }
            QLabel#pageHeading { font-size: 20px; font-weight: 700; }
            QLabel#sectionHeading { font-size: 17px; font-weight: 700; }
            QLabel#mutedLabel { color: #93a4bd; }
            QLabel#cursorTime { color: #e8effa; font-size: 14px; font-weight: 700; }
            QLabel#fileBadge, QLabel#replayBadge { background: #1d2c42;
                padding: 7px 12px; border-radius: 7px; }
            QLabel#replayBadge { background: #153c36; color: #66e1a7; }
        """)
        self.statusBar().showMessage("请选择 BLF 文件")

    def _update_window_title(self) -> None:
        name = self.project_path.name if self.project_path else "未命名项目"
        marker = " *" if self.project_dirty else ""
        self.setWindowTitle(f"CANFlow V2 · {name}{marker}")

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
            self, "保存项目", "当前项目有未保存的修改。是否保存？",
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

    def _populate_recent_projects(self) -> None:
        self.recent_project_menu.clear()
        for item in self._recent("recent/projects"):
            action = self.recent_project_menu.addAction(item)
            action.setEnabled(Path(item).is_file())
            action.triggered.connect(lambda _checked=False, value=item: self._open_project(value))
        if not self.recent_project_menu.actions():
            empty = self.recent_project_menu.addAction("暂无")
            empty.setEnabled(False)

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

    def _set_line_width(self, width: float) -> None:
        self.chart.set_line_width(width)
        self.settings.setValue("chart/line_width", width)

    def _set_grid(self, visible: bool) -> None:
        self.chart.set_grid(visible)
        self.settings.setValue("chart/grid", visible)

    def _set_theme(self, theme: str) -> None:
        self.chart.set_theme(theme)
        self.settings.setValue("chart/theme", theme)

    def _set_playback_details_visible(self, visible: bool, persist: bool = True) -> None:
        self.playback_details.setVisible(visible)
        self.details_toggle.setText("隐藏回放详情" if visible else "显示回放详情")
        self.details_toggle.setToolTip("隐藏时间轴、进度和原始报文" if visible else "显示时间轴、进度和原始报文")
        if self.details_toggle.isChecked() != visible:
            self.details_toggle.blockSignals(True)
            self.details_toggle.setChecked(visible)
            self.details_toggle.blockSignals(False)
        if persist:
            self.settings.setValue("layout/playback_details_visible", visible)

    def _set_display_settings_visible(self, visible: bool) -> None:
        self.display_panel.setVisible(visible)
        self.display_toggle.setText("显示与回放  -" if visible else "显示与回放  +")

    def _filter_curve_rows(self, query: str) -> None:
        needle = query.strip().casefold()
        shown = 0
        for row in range(self.curve_table.rowCount()):
            item = self.curve_table.item(row, 0)
            key = item.data(QtCore.Qt.ItemDataRole.UserRole)
            matches = not needle or (isinstance(key, SignalKey) and
                                     (needle in key.name.casefold() or needle in key.label().casefold()))
            self.curve_table.setRowHidden(row, not matches)
            shown += matches
        total = self.curve_table.rowCount()
        self.curve_footer.setText(
            f"显示 {shown} / {total} 个信号 · 点击聚焦 · 勾选显示" if total else "暂无信号，请到配置页选择"
        )

    def _update_curve_summary(self) -> None:
        selected = self.curve_table.rowCount()
        displayed = sum(layer.visible for layer in self.chart.layers.values())
        self.signal_count_label.setText(f"{selected} 个信号")
        self.chart_summary_label.setText(f"{selected} 条已选 · {displayed} 条显示")
        self._filter_curve_rows(self.curve_search.text())

    def _curve_visibility_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        key = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if isinstance(key, SignalKey):
            visible = item.checkState() == QtCore.Qt.CheckState.Checked
            self.chart.set_visible(key, visible)
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
                if self.cursor_x is not None:
                    self._update_cursor_values(self.cursor_x)

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
        self.statusBar().showMessage("预检失败")
        QtWidgets.QMessageBox.critical(self, "无法导入", error)

    def _scan_finished(self) -> None:
        self.scanner = None
        self.add_files.setEnabled(True)
        self.clear_files.setEnabled(True)

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
            self.channel_table.setItem(row, 1, QtWidgets.QTableWidgetItem(value))
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
        if channel is None:
            channel = self._selected_channel()
        if channel is None:
            QtWidgets.QMessageBox.information(self, "选择通道", "请先选择一个通道")
            return
        try:
            old_databases = load_databases(self.mapping)
        except Exception:
            old_databases = {}
        pending_references = dict(self.unresolved_selected)
        for key in self.selected:
            pending_references[key] = SignalReference(key, signal_definition_fingerprint(key, old_databases))
        try:
            test_mapping = {**self.mapping, channel: dbc_path.resolve()}
            databases = load_databases(test_mapping)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "DBC 无法读取", str(exc))
            return
        self._stop()
        self.mapping = test_mapping
        self.missing_mappings.pop(channel, None)
        self.unconfigured_channels.discard(channel)
        self._remember_recent("recent/dbcs", dbc_path)
        self.signals = available_signals(databases)
        available = set(self.signals)
        self.selected.clear()
        self.unresolved_selected.clear()
        for key, reference in pending_references.items():
            current = signal_definition_fingerprint(key, databases)
            if key in available and (not reference.fingerprint or current == reference.fingerprint):
                self.selected.add(key)
            else:
                self.unresolved_selected[key] = reference
        self._reset_cache()
        self._populate_signals()
        self._refresh_channel_table()
        self._mark_project_dirty()

    def _reload_available_signals(self) -> None:
        try:
            self.signals = available_signals(load_databases(self.mapping))
        except Exception:
            self.signals = []
        self.selected.intersection_update(self.signals)
        self._populate_signals()
        self._sync_curves()

    def _populate_signals(self) -> None:
        self.signal_list.blockSignals(True)
        self.signal_list.clear()
        for key in self.signals:
            item = QtWidgets.QListWidgetItem(key.label())
            item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked if key in self.selected else QtCore.Qt.CheckState.Unchecked)
            self.signal_list.addItem(item)
        for key in sorted(self.unresolved_selected, key=lambda item: item.label()):
            item = QtWidgets.QListWidgetItem(f"[未解析] {key.label()}")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
            item.setData(QtCore.Qt.ItemDataRole.UserRole + 1, True)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked)
            self.signal_list.addItem(item)
        self.signal_list.blockSignals(False)
        self._filter_signals(self.signal_search.text())
        self._update_config_signal_count()

    def _signal_selection_changed(self, _item: QtWidgets.QListWidgetItem) -> None:
        self._update_config_signal_count()
        self._mark_project_dirty()
        self.signal_timer.start()

    def _selection_from_list(self) -> tuple[set[SignalKey], dict[SignalKey, SignalReference]]:
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

    def _filter_signals(self, query: str) -> None:
        query = query.lower().strip()
        for row in range(self.signal_list.count()):
            item = self.signal_list.item(row)
            item.setHidden(query not in item.text().lower())

    def _apply_signals(self) -> None:
        chosen, unresolved = self._selection_from_list()
        new = chosen - self.cached
        self.selected = chosen
        if unresolved != self.unresolved_selected:
            self.unresolved_selected = unresolved
            self._populate_signals()
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
        visible = {key: layer.visible for key, layer in self.chart.layers.items()}
        for key in list(self.curves):
            if key not in self.selected:
                self.chart.remove_signal(key)
                self.curves.pop(key)
                self.curve_data.pop(key, None)
        try:
            databases = load_databases(self.mapping)
        except Exception:
            databases = {}
        for key in sorted(self.selected - self.curves.keys(), key=lambda item: item.label()):
            color = signal_color(key)
            self.curves[key] = self.chart.add_signal(key, key.label(), signal_unit(key, databases), color)
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
        self.selected, self.unresolved_selected = self._selection_from_list()
        self._sync_curves()
        if mark_dirty:
            self._mark_project_dirty()

    def _launch(self, signals: set[SignalKey], start_at: float | None = None, backfill: bool = False) -> None:
        self.worker = ReplayWorker(self.files, self.mapping.copy(), signals.copy(), self.cache_path, start_at, backfill)
        worker = self.worker
        worker.advanced.connect(lambda data: self._advanced(worker, data))
        worker.failed.connect(lambda error: self._worker_failed(worker, error))
        worker.completed.connect(lambda complete: self._completed(worker, complete, signals, backfill))
        worker.finished.connect(lambda: self._worker_finished(worker))
        worker.start()
        self.replay_badge.setText("补画中" if backfill else "回放中")
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
                window = self.follow_window.value()
                self.plot.setXRange(max(0, x - window), max(1.0, x), padding=0)
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
            self.replay_badge.setText("补画完成" if backfill else "回放完成")
        else:
            self.statusBar().showMessage("任务已停止")
            self.replay_badge.setText("已停止")
        self.pause_button.setText("暂停")
        self._refresh_plot()
        if complete and not backfill and self.follow.isChecked():
            self._fit_xy()
        self._update_signal_notice()

    def _worker_finished(self, worker: ReplayWorker) -> None:
        if worker is self.worker:
            self.worker = None
            missing = self.selected - self.cached
            if worker.succeeded and missing and self.files and self.played_until > self.files[0].first:
                self._set_signal_notice(f"正在补画 {len(missing)} 个新信号…")
                self._launch(missing, backfill=True)

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
        if worker is self.worker:
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

    def _stop(self) -> None:
        if self.worker:
            worker = self.worker
            worker.stop()
            worker.wait()
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
        if not self.files or not self.curves:
            return
        origin = self.files[0].first
        low, high = self.plot.viewRange()[0]
        visible_keys = [key for key in self.curves if self.chart.layers[key].visible]
        if not visible_keys:
            return
        batch_size = len(visible_keys) if all_visible else min(20, len(visible_keys))
        offset = getattr(self, "_plot_refresh_offset", 0) % len(visible_keys)
        keys = [visible_keys[(offset + index) % len(visible_keys)] for index in range(batch_size)]
        self._plot_refresh_offset = (offset + batch_size) % len(visible_keys)
        for key in keys:
            x, y = plot_points(self.db, key.storage_key(), origin + max(0, low), origin + high)
            relative_x = [value - origin for value in x]
            self.curve_data[key] = (relative_x, y)
            self.chart.set_data(key, relative_x, y)
        if self.cursor_x is not None:
            self._update_cursor_values(self.cursor_x)

    def _reset_cache(self) -> None:
        self.db.close()
        self.cache_dir.cleanup()
        self.cache_dir = tempfile.TemporaryDirectory(prefix="canflow-")
        self.cache_path = Path(self.cache_dir.name) / "samples.sqlite"
        self.db = connect(self.cache_path)
        self.cached.clear()
        self.curve_data.clear()
        self.cursor_x = None
        self.cursor_time_label.setText("—")
        self.cursor_absolute_label.setText("移动到波形查看游标时间")
        self.cursor_readout.setToolTip(self.cursor_absolute_label.text())
        self._set_signal_notice("")
        self.raw_table.setRowCount(0)
        self.progress.setValue(0)
        for curve in self.curves.values():
            curve.setData([], [])

    def _clear_recordings(self) -> None:
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

    def _clear(self) -> None:
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

    def closeEvent(self, event) -> None:
        if not self._maybe_save_project():
            event.ignore()
            return
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
