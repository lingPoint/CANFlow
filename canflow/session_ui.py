"""Widgets for one analysis session; workspace commands live above all sessions."""
from PySide6 import QtCore, QtWidgets

from .chart import MultiSignalChart


LIGHT_STYLE = """
QWidget { color: #283d4c; font-size: 13px; }
QMainWindow, QDialog { background: #edf0f2; }
QLabel { background: transparent; }
QFrame#analysisCard { background: white; border: 1px solid #c5cfd6; }
QFrame#analysisCard[active="true"] { border: 2px solid #3984ad; }
QWidget#sessionHeader { background: #f5f8fa; border-bottom: 1px solid #d8e0e5; }
QLabel#sessionTitle { font-weight: 600; color: #245e80; }
QLabel#mutedLabel { color: #667e8e; }
QLabel#replayBadge { background: #e4f1ed; color: #28684f; padding: 3px 8px; border-radius: 3px; }
QLabel#brand { font-size: 17px; font-weight: 700; }
QToolBar { background: white; border: 0; border-bottom: 1px solid #d1dbe2; spacing: 5px; padding: 5px; }
QPushButton, QToolButton, QComboBox, QSpinBox, QDoubleSpinBox {
    background: white; border: 1px solid #c4cfd8; border-radius: 4px;
    padding: 5px 9px; min-height: 22px;
}
QPushButton:hover, QToolButton:hover { background: #edf6fb; border-color: #6c9fbc; }
QPushButton:checked, QToolButton:checked { background: #dceefa; border-color: #3984ad; }
QPushButton:disabled, QToolButton:disabled { color: #9aa8b2; background: #f4f6f8; }
QPushButton#primaryButton { background: #397fa5; color: white; border-color: #397fa5; }
QPushButton#primaryButton:disabled { background: #dfe6eb; border-color: #dfe6eb; color: #899ba7; }
QLineEdit { background: white; border: 1px solid #cbd6de; border-radius: 3px; padding: 5px 8px; }
QLineEdit:focus { border-color: #3984ad; }
QTableWidget, QListWidget { background: white; alternate-background-color: #f7f9fb;
    border: 0; gridline-color: #e2e8ec; selection-background-color: #e0eef7; selection-color: #244e68; }
QTableWidget::item { padding: 3px; border-bottom: 1px solid #edf1f4; }
QListWidget::item { padding: 0 8px; border-bottom: 1px solid #edf1f4; }
QHeaderView::section { background: #f3f6f8; color: #637a89; padding: 6px; border: 0;
    border-bottom: 1px solid #dce4e9; }
QScrollArea { background: transparent; border: 0; }
QSplitter::handle { background: #e3e9ed; }
QMenu { background: white; border: 1px solid #cbd6de; padding: 4px; }
QMenu::item { padding: 7px 22px; }
QMenu::item:selected { background: #e0eef7; }
QStatusBar { background: #f5f8fa; color: #617887; }
QProgressBar { border: 0; background: #e5ecf0; }
QProgressBar::chunk { background: #3984ad; }
QGroupBox { border: 1px solid #cbd6de; margin-top: 14px; padding: 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
"""


def button(text, callback, parent=None):
    widget = QtWidgets.QPushButton(text, parent)
    widget.clicked.connect(callback)
    return widget


def table(headers):
    widget = QtWidgets.QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.horizontalHeader().setStretchLastSection(True)
    widget.verticalHeader().hide()
    widget.verticalHeader().setDefaultSectionSize(32)
    widget.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setAlternatingRowColors(True)
    widget.setShowGrid(False)
    return widget


def dialog(session, title, size=(760, 520)):
    widget = QtWidgets.QDialog(session)
    widget.setWindowTitle(f"分析窗口 {session.session_id} · {title}")
    widget.resize(*size)
    layout = QtWidgets.QVBoxLayout(widget)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(10)
    return widget, layout


def build_session_ui(s, delegate):
    """Build the real analysis canvas and its modeless, session-owned editors."""
    s.setStyleSheet(LIGHT_STYLE)
    central = QtWidgets.QWidget()
    s.setCentralWidget(central)
    root = QtWidgets.QVBoxLayout(central)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)
    s.card = QtWidgets.QFrame()
    s.card.setObjectName("analysisCard")
    root.addWidget(s.card)
    card_layout = QtWidgets.QVBoxLayout(s.card)
    card_layout.setContentsMargins(2, 2, 2, 2)
    card_layout.setSpacing(0)
    s.session_header = QtWidgets.QWidget()
    s.session_header.setObjectName("sessionHeader")
    header = QtWidgets.QHBoxLayout(s.session_header)
    header.setContentsMargins(10, 6, 10, 6)
    s.title_label = QtWidgets.QLabel(f"数据分析窗口 {s.session_id}")
    s.title_label.setObjectName("sessionTitle")
    header.addWidget(s.title_label)
    s.replay_badge = QtWidgets.QLabel("等待导入")
    s.replay_badge.setObjectName("replayBadge")
    header.addWidget(s.replay_badge)
    s.file_badge = QtWidgets.QLabel("未导入 BLF")
    s.file_badge.setMaximumWidth(240)
    header.addWidget(s.file_badge)
    s.mapping_badge = QtWidgets.QLabel("DBC：未配置")
    header.addWidget(s.mapping_badge)
    header.addStretch()
    s.project_label = QtWidgets.QLabel("未命名项目")
    s.project_label.setObjectName("mutedLabel")
    s.project_label.setMaximumWidth(240)
    s.project_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
    s.project_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
    header.addWidget(s.project_label)
    card_layout.addWidget(s.session_header)
    s.wave_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
    s.wave_splitter.setChildrenCollapsible(False)
    card_layout.addWidget(s.wave_splitter, 1)
    s.sidebar = QtWidgets.QWidget()
    s.sidebar.setMinimumWidth(230)
    sidebar = QtWidgets.QVBoxLayout(s.sidebar)
    sidebar.setContentsMargins(8, 8, 8, 6)
    sidebar.setSpacing(5)
    s.curve_search = QtWidgets.QLineEdit()
    s.curve_search.setPlaceholderText("搜索信号 / 报文 ID")
    s.curve_search.setClearButtonEnabled(True)
    s.curve_search.textChanged.connect(s._filter_curve_rows)
    sidebar.addWidget(s.curve_search)
    s.curve_table = table(["显示", "名称", "游标值"])
    s.curve_table.setColumnWidth(0, 44)
    s.curve_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
    s.curve_table.setColumnWidth(2, 95)
    s.curve_table.itemChanged.connect(s._curve_visibility_changed)
    s.curve_table.currentCellChanged.connect(s._curve_focus_changed)
    sidebar.addWidget(s.curve_table, 1)
    s.curve_footer = QtWidgets.QLabel("暂无信号，请从顶部添加信号")
    s.curve_footer.setObjectName("mutedLabel")
    sidebar.addWidget(s.curve_footer)
    s.signal_count_label = QtWidgets.QLabel(s)
    s.signal_count_label.hide()
    s.wave_splitter.addWidget(s.sidebar)
    right = QtWidgets.QWidget()
    right_layout = QtWidgets.QVBoxLayout(right)
    right_layout.setContentsMargins(8, 6, 8, 4)
    right_layout.setSpacing(4)
    readouts = QtWidgets.QHBoxLayout()
    s.chart_summary_label = QtWidgets.QLabel("0 条已选 · 0 条显示")
    s.chart_summary_label.setObjectName("mutedLabel")
    readouts.addWidget(s.chart_summary_label)
    s.measure_label = QtWidgets.QLabel("")
    s.measure_label.setObjectName("mutedLabel")
    s.measure_label.setMinimumWidth(0)
    s.measure_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
    readouts.addWidget(s.measure_label, 1)
    s.cursor_readout = QtWidgets.QWidget()
    cursor_layout = QtWidgets.QHBoxLayout(s.cursor_readout)
    cursor_layout.setContentsMargins(0, 0, 0, 0)
    cursor_layout.addWidget(QtWidgets.QLabel("游标"))
    s.cursor_time_label = QtWidgets.QLabel("—")
    cursor_layout.addWidget(s.cursor_time_label)
    s.cursor_absolute_label = QtWidgets.QLabel(s)
    s.cursor_absolute_label.hide()
    readouts.addWidget(s.cursor_readout)
    right_layout.addLayout(readouts)
    s.chart = MultiSignalChart()
    s.plot = s.chart.widget
    s.chart.cursor_moved.connect(s._update_cursor_values)
    s.chart.measurement_clicked.connect(s._measure_at)
    right_layout.addWidget(s.chart, 1)
    s.wave_splitter.addWidget(right)
    s.wave_splitter.setSizes([310, 1100])
    s.wave_splitter.setStretchFactor(1, 1)
    build_editors(s, delegate)
    build_toolbar(s)
    s.statusBar().hide()
    s.statusBar().showMessage("从顶部导入 BLF，配置 DBC 并添加信号")
    s._update_workflow()


def build_editors(s, delegate):
    s.files_dialog, files = dialog(s, "记录文件")
    row = QtWidgets.QHBoxLayout()
    s.add_files = button("导入 BLF", s._add_files)
    s.clear_files = button("清空记录", s._clear_recordings)
    row.addWidget(s.add_files)
    row.addWidget(s.clear_files)
    row.addStretch()
    files.addLayout(row)
    files.addWidget(QtWidgets.QLabel("按首帧采集时间排序，逐个文件回放。导入失败或取消会保留原列表。"))
    s.file_table = table(["文件", "首帧时间", "帧数"])
    s.file_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
    s.file_table.setColumnWidth(1, 200)
    files.addWidget(s.file_table, 1)
    s.file_summary = QtWidgets.QLabel()
    files.addWidget(s.file_summary)

    s.mapping_dialog, mapping = dialog(s, "配置 DBC")
    mapping.addWidget(QtWidgets.QLabel("为当前分析窗口的 CAN 通道指定 DBC；未配置的通道仍可查看原始报文。"))
    s.channel_table = table(["通道", "DBC"])
    s.channel_table.setColumnWidth(0, 120)
    mapping.addWidget(s.channel_table, 1)
    s.mapping_controls = QtWidgets.QWidget()
    row = QtWidgets.QHBoxLayout(s.mapping_controls)
    row.setContentsMargins(0, 0, 0, 0)
    for title, callback in (("新增通道", s._add_channel_mapping), ("指定 DBC", s._assign_dbc),
                            ("删除映射", s._remove_channel_mapping)):
        row.addWidget(button(title, callback))
    recent = QtWidgets.QToolButton()
    recent.setText("最近 DBC")
    recent.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
    s.recent_dbc_menu = QtWidgets.QMenu(recent)
    s.recent_dbc_menu.aboutToShow.connect(s._populate_recent_dbcs)
    recent.setMenu(s.recent_dbc_menu)
    row.addWidget(recent)
    mapping.addWidget(s.mapping_controls)

    s.signals_dialog, signals = dialog(s, "添加信号", (800, 650))
    signals.addWidget(QtWidgets.QLabel("勾选自动生效；回放中新增信号将在处理结束后自动补画。"))
    s.signal_search = QtWidgets.QLineEdit()
    s.signal_search.setPlaceholderText("搜索信号 / 报文 ID")
    s.signal_search.setClearButtonEnabled(True)
    s.signal_search.textChanged.connect(s._filter_signals)
    signals.addWidget(s.signal_search)
    s.config_signal_count = QtWidgets.QLabel("0 个已选")
    signals.addWidget(s.config_signal_count)
    s.signal_list = QtWidgets.QListWidget()
    s.signal_list.setItemDelegate(delegate(s.signal_list))
    s.signal_list.itemChanged.connect(s._signal_selection_changed)
    signals.addWidget(s.signal_list, 1)
    s.selection_summary = QtWidgets.QLabel("当前未选择信号")
    s.selection_summary.setWordWrap(True)
    signals.addWidget(s.selection_summary)
    row = QtWidgets.QHBoxLayout()
    s.group_combo = QtWidgets.QComboBox()
    row.addWidget(s.group_combo, 1)
    row.addWidget(button("替换", lambda: s._apply_signal_group(False)))
    row.addWidget(button("追加", lambda: s._apply_signal_group(True)))
    signals.addLayout(row)
    row = QtWidgets.QHBoxLayout()
    for title, callback in (("保存为信号组", s._save_signal_group), ("删除组", s._delete_signal_group),
                            ("导入组", s._import_signal_group), ("导出组", s._export_signal_group)):
        row.addWidget(button(title, callback))
    signals.addLayout(row)
    s._populate_group_combo()
    s.notice_toggle = QtWidgets.QToolButton()
    s.notice_toggle.setCheckable(True)
    s.notice_toggle.toggled.connect(s._toggle_signal_notice)
    signals.addWidget(s.notice_toggle)
    s.signal_notice = QtWidgets.QLabel()
    s.signal_notice.setWordWrap(True)
    s.signal_notice.setTextFormat(QtCore.Qt.TextFormat.PlainText)
    s.signal_notice.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
    s.notice_scroll = QtWidgets.QScrollArea()
    s.notice_scroll.setWidgetResizable(True)
    s.notice_scroll.setWidget(s.signal_notice)
    s.notice_scroll.setMaximumHeight(140)
    signals.addWidget(s.notice_scroll)
    s.notice_toggle.hide()
    s.notice_scroll.hide()

    s.display_panel, settings = dialog(s, "显示与回放设置", (520, 400))
    form = QtWidgets.QFormLayout()
    settings.addLayout(form)
    s.time_mode_combo = QtWidgets.QComboBox()
    s.time_mode_combo.addItem("相对时间", "relative")
    s.time_mode_combo.addItem("原始采集时间", "capture")
    s.time_mode_combo.setCurrentIndex(1 if s.settings.value("chart/time_mode", "relative") == "capture" else 0)
    s.chart.set_time_mode(s.time_mode_combo.currentData())
    s.time_mode_combo.currentIndexChanged.connect(s._set_time_mode)
    form.addRow("时间轴", s.time_mode_combo)
    s.sample_emphasis = QtWidgets.QDoubleSpinBox()
    s.sample_emphasis.setRange(0.5, 8.0)
    s.sample_emphasis.setSingleStep(0.5)
    s.sample_emphasis.setValue(s.settings.value("chart/sample_emphasis", 2.5, type=float))
    s.chart.set_sample_emphasis(s.sample_emphasis.value())
    s.sample_emphasis.valueChanged.connect(s._set_sample_emphasis)
    form.addRow("采样点粗细", s.sample_emphasis)
    s.lock_y = QtWidgets.QCheckBox("锁定全部 Y 轴")
    s.lock_y.setChecked(s.settings.value("chart/y_locked", False, type=bool))
    s.chart.y_locked = s.lock_y.isChecked()
    s.lock_y.toggled.connect(s._set_y_locked)
    form.addRow(button("自动适配 Y", s._reset_auto_y), s.lock_y)
    s.grid_toggle = QtWidgets.QCheckBox("显示网格")
    s.grid_toggle.setChecked(s.settings.value("chart/grid", True, type=bool))
    s.chart.set_grid(s.grid_toggle.isChecked())
    s.grid_toggle.toggled.connect(s._set_grid)
    s.theme_combo = QtWidgets.QComboBox()
    s.theme_combo.addItems(["浅色", "深色"])
    s.theme_combo.setCurrentText(s.settings.value("workspace/chart_theme", "浅色", type=str))
    s.chart.set_theme(s.theme_combo.currentText())
    s.theme_combo.currentTextChanged.connect(s._set_theme)
    form.addRow(s.grid_toggle, s.theme_combo)
    s.follow = QtWidgets.QCheckBox("跟随播放")
    s.follow.setChecked(s.settings.value("chart/follow", True, type=bool))
    s.follow.toggled.connect(lambda v: s.settings.setValue("chart/follow", v))
    s.follow_window = QtWidgets.QSpinBox()
    s.follow_window.setRange(5, 3600)
    s.follow_window.setSuffix(" 秒")
    s.follow_window.setValue(s.settings.value("chart/follow_window", 60, type=int))
    s.follow_window.valueChanged.connect(lambda v: s.settings.setValue("chart/follow_window", v))
    form.addRow(s.follow, s.follow_window)
    settings.addWidget(QtWidgets.QLabel("Ctrl + 滚轮：同步缩放各轨道纵轴\n显示设置会记住，已打开窗口的当前视图互不影响。"))
    settings.addStretch()

    s.playback_details, details = dialog(s, "原始报文与进度", (950, 530))
    details.addWidget(QtWidgets.QLabel("最近 1000 条 CAN / CAN FD 报文；完整记录保留在原始 BLF 文件中。"))
    s.raw_table = table(["时间", "通道", "ID", "类型", "数据"])
    s.raw_table.setColumnWidth(0, 205)
    details.addWidget(s.raw_table, 1)
    s.time_label = QtWidgets.QLabel("—")
    details.addWidget(s.time_label)
    s.timeline = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
    s.timeline.setRange(0, 10000)
    s.timeline.sliderReleased.connect(s._seek)
    details.addWidget(s.timeline)
    s.progress = QtWidgets.QProgressBar()
    s.progress.setRange(0, 1000)
    s.progress.setFixedHeight(6)
    s.progress.setTextVisible(False)
    details.addWidget(s.progress)


def build_toolbar(s):
    # The workspace mounts exactly one of these toolbars for its active session.
    s.commands = QtWidgets.QToolBar("分析操作", s)
    s.commands.setMovable(False)
    s.commands.addWidget(button("导入 BLF", s._add_files))
    s.import_button = s.commands.widgetForAction(s.commands.actions()[-1])
    s.commands.addWidget(button("文件管理", lambda: s.show_editor("files")))
    s.commands.addWidget(button("配置 DBC", lambda: s.show_editor("mapping")))
    s.commands.addWidget(button("添加信号", lambda: s.show_editor("signals")))
    s.commands.addSeparator()
    s.start_button = button("开始回放", s._start)
    s.start_button.setObjectName("primaryButton")
    s.pause_button = button("暂停", s._pause)
    s.stop_button = button("停止", s._stop_tasks)
    s.stop_button.setToolTip("停止当前窗口的预检、DBC 导入或回放")
    for widget in (s.start_button, s.pause_button, s.stop_button):
        s.commands.addWidget(widget)
    s.commands.addSeparator()
    s.fit_button = button("自动适配", s._fit_xy)
    s.measure_toggle = button("测量间隔", lambda: None)
    s.measure_toggle.setCheckable(True)
    s.measure_toggle.toggled.connect(s._set_measurement_enabled)
    s.measure_toggle.setToolTip("选择左侧信号后，在波形上依次点击两个真实采样点")
    for widget in (s.fit_button, s.measure_toggle, button("清除标记", s._clear_measurement)):
        s.commands.addWidget(widget)
    s.commands.addSeparator()
    s.display_toggle = button("显示设置", lambda: s.show_editor("display"))
    s.details_toggle = button("原始报文", lambda: s.show_editor("details"))
    s.commands.addWidget(s.display_toggle)
    s.commands.addWidget(s.details_toggle)
    s.addToolBar(s.commands)  # Also usable as a standalone session for diagnostics.
