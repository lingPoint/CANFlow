"""Multi-window workspace: routes commands, never owns decoded recording data."""
from PySide6 import QtCore, QtGui, QtWidgets

from . import __version__
from .session_ui import LIGHT_STYLE, button


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        from .app import logo_pixmap

        self.setWindowTitle(f"CANFlow v{__version__} · 多窗口数据分析")
        self.setWindowIcon(QtGui.QIcon(logo_pixmap(256)))
        self.resize(1500, 940)
        self.setMinimumSize(960, 620)
        self.setStyleSheet(LIGHT_STYLE)
        self.sessions = []
        self.active_session = None
        self._next_id = 1
        self._closing = False
        self._build_ui()
        self.add_analysis_window()
        self.add_analysis_window()
        self.activate_session(self.sessions[0])
        QtWidgets.QApplication.instance().installEventFilter(self)

    def _build_ui(self):
        bar = QtWidgets.QToolBar("工作区", self)
        bar.setMovable(False)
        self.addToolBar(bar)
        brand = QtWidgets.QLabel(f"CANFlow v{__version__}")
        brand.setObjectName("brand")
        brand.setContentsMargins(7, 0, 12, 0)
        bar.addWidget(brand)
        self.new_window_button = button("＋ 新增窗口", self.add_analysis_window)
        bar.addWidget(self.new_window_button)
        self.close_window_button = button("关闭窗口", self.close_active_window)
        bar.addWidget(self.close_window_button)
        bar.addSeparator()
        self.project_button = QtWidgets.QToolButton()
        self.project_button.setText("项目")
        self.project_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.project_menu = QtWidgets.QMenu(self.project_button)
        for title, shortcut, method in (
            ("新建当前窗口项目", "Ctrl+N", "_new_project"),
            ("打开项目…", "Ctrl+O", "_open_project"),
            ("保存项目", "Ctrl+S", "_save_project"),
            ("项目另存为…", "Ctrl+Shift+S", "_save_project_as"),
        ):
            action = self.project_menu.addAction(title)
            action.setShortcut(shortcut)
            action.triggered.connect(lambda checked=False, name=method: self._project_command(name))
            self.addAction(action)
        self.recent_project_menu = self.project_menu.addMenu("最近项目")
        self.project_menu.aboutToShow.connect(self._populate_recent_projects)
        self.project_button.setMenu(self.project_menu)
        bar.addWidget(self.project_button)
        bar.addWidget(button("保存项目", lambda: self._project_command("_save_project")))
        bar.addSeparator()
        bar.addWidget(QtWidgets.QLabel("操作对象"))
        self.window_selector = QtWidgets.QComboBox()
        self.window_selector.setMinimumWidth(175)
        self.window_selector.currentIndexChanged.connect(self._select_index)
        bar.addWidget(self.window_selector)
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        self.window_count = QtWidgets.QLabel()
        bar.addWidget(self.window_count)
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.command_stack = QtWidgets.QStackedWidget()
        self.command_stack.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        layout.addWidget(self.command_stack)
        self.notice = QtWidgets.QLabel("点击任一分析窗口，将顶部操作切换到该窗口。")
        self.notice.setContentsMargins(14, 5, 14, 5)
        self.notice.setStyleSheet("background: #eaf3f8; color: #52758a;")
        layout.addWidget(self.notice)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.canvas = QtWidgets.QWidget()
        self.canvas_layout = QtWidgets.QVBoxLayout(self.canvas)
        self.canvas_layout.setContentsMargins(10, 10, 10, 10)
        self.canvas_layout.setSpacing(10)
        self.scroll.setWidget(self.canvas)
        layout.addWidget(self.scroll, 1)
        for sequence, callback in (("Ctrl+Shift+N", self.add_analysis_window),
                                   ("Ctrl+W", self.close_active_window),
                                   ("Ctrl+Tab", self.next_window)):
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(sequence), self)
            shortcut.activated.connect(callback)
        self.statusBar().showMessage("各窗口的 BLF、DBC、信号、游标与回放任务相互独立")

    def add_analysis_window(self):
        from .app import AnalysisWindow

        session = AnalysisWindow(self._next_id)
        self._next_id += 1
        session.project_active = True
        session.setWindowFlags(QtCore.Qt.WindowType.Widget)
        session.setMinimumHeight(300)
        session.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self.sessions.append(session)
        self.canvas_layout.addWidget(session, 1)
        session.removeToolBar(session.commands)
        self.command_stack.addWidget(session.commands)
        session.state_changed.connect(lambda: self._session_changed(session))
        session.statusBar().messageChanged.connect(lambda text: self._status_changed(session, text))
        self.window_selector.blockSignals(True)
        self.window_selector.addItem(f"分析窗口 {session.session_id}")
        self.window_selector.blockSignals(False)
        session.show()
        self.activate_session(session)
        self._update_workspace()
        return session

    def _update_workspace(self):
        self.window_count.setText(f"{len(self.sessions)} 个分析窗口  ")
        self.close_window_button.setEnabled(len(self.sessions) > 1)

    def activate_session(self, session):
        if self._closing or session not in self.sessions:
            return
        self.active_session = session
        self.command_stack.setCurrentWidget(session.commands)
        self.window_selector.blockSignals(True)
        self.window_selector.setCurrentIndex(self.sessions.index(session))
        self.window_selector.blockSignals(False)
        for item in self.sessions:
            active = item is session
            if item.card.property("active") != active:
                item.card.setProperty("active", active)
                item.card.style().unpolish(item.card)
                item.card.style().polish(item.card)
                item.card.update()
            item.title_label.setText(f"数据分析窗口 {item.session_id}" + (" · 当前" if active else ""))
        self._session_changed(session)
        self._status_changed(session, session.statusBar().currentMessage())

    def _select_index(self, index):
        if 0 <= index < len(self.sessions):
            session = self.sessions[index]
            self.activate_session(session)
            self.scroll.ensureWidgetVisible(session)

    def next_window(self):
        if self.sessions:
            index = (self.sessions.index(self.active_session) + 1) % len(self.sessions)
            self._select_index(index)

    def _session_changed(self, session):
        if self._closing or session not in self.sessions:
            return
        index = self.sessions.index(session)
        marker = " *" if session.project_dirty else ""
        self.window_selector.setItemText(index, f"分析窗口 {session.session_id}{marker}")
        if session is self.active_session:
            self.notice.setText(f"当前操作：分析窗口 {session.session_id}  ·  "
                                f"{len(session.files)} 个 BLF  ·  {len(session.mapping)} 个 DBC 通道  ·  "
                                f"{len(session.selected)} 个已选信号  |  点击其他窗口可切换")
            self.project_button.setToolTip(session.windowTitle())

    def _status_changed(self, session, text):
        if not self._closing and session is self.active_session:
            self.statusBar().showMessage(f"窗口 {session.session_id}：{text}")

    def eventFilter(self, watched, event):
        if not self._closing and event.type() in (QtCore.QEvent.Type.MouseButtonPress,
                                                  QtCore.QEvent.Type.FocusIn):
            # Walk parents, including a modeless editor, to find the true owner.
            widget = watched if isinstance(watched, QtWidgets.QWidget) else None
            while widget is not None:
                if widget in self.sessions:
                    if widget is not self.active_session:
                        self.activate_session(widget)
                    break
                widget = widget.parentWidget()
        return super().eventFilter(watched, event)

    def _project_command(self, name):
        session = self.active_session
        if session is None:
            return
        if name in ("_new_project", "_open_project") and (session.worker or session.scanner or session.dbc_loader):
            self.statusBar().showMessage(f"窗口 {session.session_id} 正在处理，请先停止任务再切换项目")
            return
        getattr(session, name)()
        session._update_workflow()

    def _populate_recent_projects(self):
        self.recent_project_menu.clear()
        session = self.active_session
        if session:
            busy = bool(session.worker or session.scanner or session.dbc_loader)
            for path in session._recent("recent/projects"):
                action = self.recent_project_menu.addAction(path)
                action.setEnabled(not busy)
                action.triggered.connect(lambda checked=False, p=path, owner=session: self._open_recent(owner, p))

    def _open_recent(self, session, path):
        if session in self.sessions and not (session.worker or session.scanner or session.dbc_loader):
            session._open_project(path)
            session._update_workflow()

    def close_active_window(self):
        if len(self.sessions) <= 1:
            self.statusBar().showMessage("至少保留一个分析窗口；可以通过文件管理清空记录")
            return False
        session = self.active_session
        if not session._maybe_save_project():
            return False
        index = self.sessions.index(session)
        session.shutdown()
        self.sessions.remove(session)
        self.canvas_layout.removeWidget(session)
        self.command_stack.removeWidget(session.commands)
        session.commands.deleteLater()
        session.hide()
        session.deleteLater()
        self.window_selector.blockSignals(True)
        self.window_selector.removeItem(index)
        self.window_selector.blockSignals(False)
        self.activate_session(self.sessions[min(index, len(self.sessions) - 1)])
        self._update_workspace()
        return True

    def open_compare(self):
        from .compare import CompareDialog

        comparison = CompareDialog(self)
        comparison.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        comparison.show()

    def closeEvent(self, event):
        # Ask about every unsaved project before destroying any session.
        for session in self.sessions:
            if not session._maybe_save_project():
                event.ignore()
                return
        self._closing = True
        QtWidgets.QApplication.instance().removeEventFilter(self)
        from .compare import CompareDialog
        for comparison in self.findChildren(CompareDialog):
            comparison.close()
        for session in self.sessions:
            session.shutdown()
        super().closeEvent(event)
