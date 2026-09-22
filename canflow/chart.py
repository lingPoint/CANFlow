from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from .core import SignalKey


PALETTE = (
    "#38bdf8", "#fb7185", "#a3e635", "#fbbf24", "#c084fc", "#2dd4bf",
    "#f97316", "#60a5fa", "#e879f9", "#4ade80", "#facc15", "#f472b6",
)


def signal_color(key: SignalKey) -> str:
    digest = hashlib.sha256(key.storage_key().encode("utf-8")).digest()
    return PALETTE[int.from_bytes(digest[:2], "big") % len(PALETTE)]


class TimelineViewBox(pg.ViewBox):
    def __init__(self, vertical_zoom: Callable[[float], None]):
        super().__init__(enableMenu=False)
        self.vertical_zoom = vertical_zoom
        self.setMouseEnabled(x=True, y=False)

    def wheelEvent(self, event, axis=None):  # noqa: N802 - Qt/pyqtgraph API
        modifiers = event.modifiers() if hasattr(event, "modifiers") else QtCore.Qt.KeyboardModifier.NoModifier
        if modifiers & QtCore.Qt.KeyboardModifier.ControlModifier:
            delta = event.delta() if hasattr(event, "delta") else event.angleDelta().y()
            self.vertical_zoom(0.82 if delta > 0 else 1.22)
            event.accept()
            return
        super().wheelEvent(event, axis=axis)


class TrackAxisPanel(QtWidgets.QWidget):
    """Paint one compact Y scale per vertically stacked signal track."""

    def __init__(self, chart: MultiSignalChart | None = None):
        super().__init__()
        self.chart = chart
        self.dark = True
        self.setFixedWidth(122)

    def set_theme(self, theme: str) -> None:
        self.dark = theme == "深色"
        self.update()

    @staticmethod
    def _number(value: float) -> str:
        return f"{value:.5g}"

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        if self.chart is None:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        background = QtGui.QColor("#111827" if self.dark else "#ffffff")
        foreground = QtGui.QColor("#d1d5db" if self.dark else "#1f2937")
        painter.fillRect(self.rect(), background)
        scene_rect = self.chart.master_view.sceneBoundingRect()
        for key, layer in self.chart.layers.items():
            if not layer.visible:
                continue
            rect = layer.view.geometry()
            top = round(rect.top() - scene_rect.top())
            bottom = round(rect.bottom() - scene_rect.top())
            if bottom <= top:
                continue
            color = QtGui.QColor(layer.color)
            if key == self.chart.focused:
                painter.setPen(QtGui.QPen(color, 2))
                painter.drawRect(QtCore.QRect(1, top + 1, self.width() - 3, max(1, bottom - top - 2)))
            axis_x = self.width() - 5
            painter.setPen(QtGui.QPen(color, 1))
            painter.drawLine(axis_x, top, axis_x, bottom)
            low, high = layer.view.viewRange()[1]
            lane_height = bottom - top
            ticks = 3 if lane_height >= 48 else 2
            for index in range(ticks):
                ratio = index / max(1, ticks - 1)
                y = round(bottom - ratio * lane_height)
                value = low + ratio * (high - low)
                painter.setPen(QtGui.QPen(color, 1))
                painter.drawLine(axis_x - 5, y, axis_x, y)
                painter.setPen(foreground)
                painter.drawText(QtCore.QRect(48, y - 9, axis_x - 57, 18),
                                 QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
                                 self._number(value))
            painter.save()
            painter.translate(12, bottom - 3)
            painter.rotate(-90)
            font = painter.font()
            font.setBold(key == self.chart.focused)
            painter.setFont(font)
            painter.setPen(color)
            title = layer.key.name + (f" [{layer.unit}]" if layer.unit else "")
            title = painter.fontMetrics().elidedText(
                title, QtCore.Qt.TextElideMode.ElideRight, max(24, lane_height - 6)
            )
            painter.drawText(QtCore.QRect(0, -10, max(24, lane_height - 6), 20),
                             QtCore.Qt.AlignmentFlag.AlignVCenter, title)
            painter.restore()


@dataclass
class CurveLayer:
    key: SignalKey
    label: str
    unit: str
    color: str
    view: pg.ViewBox
    curve: pg.PlotDataItem
    visible: bool = True
    range_initialized: bool = False


class MultiSignalChart(QtWidgets.QWidget):
    """One capture timeline with an independent ViewBox for every signal."""

    cursor_moved = QtCore.Signal(float)
    ranges_changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layers: dict[SignalKey, CurveLayer] = {}
        self.focused: SignalKey | None = None
        self.auto_scale = True
        self.y_locked = False
        self.line_width = 1.4
        self.theme = "深色"
        self.master_view = TimelineViewBox(self.scale_visible_y)
        self.widget = pg.PlotWidget(viewBox=self.master_view, background="#111827")
        self.widget.showGrid(x=True, y=True, alpha=0.2)
        self.widget.setLabel("bottom", "相对首帧时间", units="s")
        self.widget.getPlotItem().hideAxis("left")
        self.widget.getPlotItem().hideButtons()
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.axis_panel = TrackAxisPanel(self)
        layout.addWidget(self.axis_panel)
        layout.addWidget(self.widget, 1)
        self.cursor = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#94a3b8", width=1))
        self.cursor.hide()
        self.master_view.addItem(self.cursor, ignoreBounds=True)
        self.widget.getPlotItem().vb.sigResized.connect(self._sync_geometry)
        self._mouse_proxy = pg.SignalProxy(
            self.widget.scene().sigMouseMoved, rateLimit=30, slot=self._mouse_moved
        )

    def add_signal(self, key: SignalKey, label: str, unit: str, color: str | None = None) -> pg.PlotDataItem:
        if key in self.layers:
            return self.layers[key].curve
        view = pg.ViewBox(enableMenu=False)
        view.setMouseEnabled(x=False, y=False)
        self.widget.scene().addItem(view)
        view.setXLink(self.master_view)
        curve = pg.PlotDataItem([], [], pen=pg.mkPen(color or signal_color(key), width=self.line_width),
                                autoDownsample=True, clipToView=True)
        view.addItem(curve)
        layer = CurveLayer(key, label, unit, color or signal_color(key), view, curve)
        self.layers[key] = layer
        self._sync_geometry()
        if self.focused is None:
            self.focus(key)
        return curve

    def remove_signal(self, key: SignalKey) -> None:
        layer = self.layers.pop(key, None)
        if layer is None:
            return
        layer.view.removeItem(layer.curve)
        self.widget.scene().removeItem(layer.view)
        if self.focused == key:
            self.focused = next(iter(self.layers), None)
            self._link_focus_axis()
        self._sync_geometry()

    def clear(self) -> None:
        for key in list(self.layers):
            self.remove_signal(key)

    def focus(self, key: SignalKey) -> None:
        if key not in self.layers:
            return
        self.focused = key
        self._link_focus_axis()

    def _link_focus_axis(self) -> None:
        for key, layer in self.layers.items():
            color = layer.color if key == self.focused else "#334155"
            width = 2 if key == self.focused else 1
            layer.view.setBorder(pg.mkPen(color, width=width))
        self.axis_panel.update()

    def set_visible(self, key: SignalKey, visible: bool) -> None:
        layer = self.layers.get(key)
        if layer is None:
            return
        layer.visible = visible
        layer.view.setVisible(visible)
        if not visible and self.focused == key:
            replacement = next((item.key for item in self.layers.values() if item.visible), None)
            self.focused = replacement
            self._link_focus_axis()
        self._sync_geometry()

    def set_data(self, key: SignalKey, x: list[float], y: list[float]) -> None:
        layer = self.layers.get(key)
        if layer is None:
            return
        layer.curve.setData(x, y)
        if self.auto_scale and (not self.y_locked or not layer.range_initialized):
            self._fit_layer_y(layer)
        self._update_axis(layer)

    def _fit_layer_y(self, layer: CurveLayer) -> None:
        y = layer.curve.yData
        if not layer.visible or y is None or len(y) == 0:
            return
        low, high = float(min(y)), float(max(y))
        padding = max(abs(low) * 0.05, 1.0) if low == high else (high - low) * 0.08
        layer.view.setYRange(low - padding, high + padding, padding=0)
        layer.range_initialized = True

    def scale_visible_y(self, factor: float) -> None:
        if self.y_locked:
            return
        self.auto_scale = False
        for layer in self.layers.values():
            if not layer.visible:
                continue
            low, high = layer.view.viewRange()[1]
            center = (low + high) / 2
            half = max((high - low) * factor / 2, 1e-12)
            layer.view.setYRange(center - half, center + half, padding=0)
            layer.range_initialized = True
            self._update_axis(layer)
        self.ranges_changed.emit()

    def reset_auto_scale(self) -> None:
        self.auto_scale = True
        self.y_locked = False
        for layer in self.layers.values():
            self._fit_layer_y(layer)
        self.axis_panel.update()
        self.ranges_changed.emit()

    def set_line_width(self, width: float) -> None:
        self.line_width = width
        for layer in self.layers.values():
            layer.curve.setPen(pg.mkPen(layer.color, width=width))

    def set_grid(self, visible: bool) -> None:
        self.widget.showGrid(x=visible, y=visible, alpha=0.2)

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        background = "#111827" if theme == "深色" else "#ffffff"
        foreground = "#d1d5db" if theme == "深色" else "#1f2937"
        self.widget.setBackground(background)
        for axis_name in ("left", "bottom"):
            self.widget.getPlotItem().getAxis(axis_name).setTextPen(foreground)
        self.axis_panel.set_theme(theme)

    def y_range(self, key: SignalKey) -> tuple[float, float]:
        layer = self.layers[key]
        low, high = layer.view.viewRange()[1]
        return float(low), float(high)

    def set_y_range(self, key: SignalKey, low: float, high: float) -> None:
        layer = self.layers.get(key)
        if layer is not None and high > low:
            layer.view.setYRange(low, high, padding=0)
            layer.range_initialized = True
            self._update_axis(layer)

    def visible_axis_keys(self) -> list[SignalKey]:
        return [key for key, layer in self.layers.items() if layer.visible]

    def _update_axis(self, layer: CurveLayer) -> None:
        del layer
        self.axis_panel.update()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        QtCore.QTimer.singleShot(0, self._sync_geometry)

    def _sync_geometry(self) -> None:
        rect = self.master_view.sceneBoundingRect()
        visible_layers = [layer for layer in self.layers.values() if layer.visible]
        if not visible_layers:
            self.axis_panel.update()
            return
        lane_height = rect.height() / len(visible_layers)
        for index, layer in enumerate(visible_layers):
            lane = QtCore.QRectF(rect.left(), rect.top() + index * lane_height, rect.width(), lane_height)
            layer.view.setGeometry(lane)
            layer.view.linkedViewChanged(self.master_view, layer.view.XAxis)
        self.axis_panel.update()

    def _mouse_moved(self, event) -> None:
        position = event[0]
        if not self.widget.sceneBoundingRect().contains(position):
            self.cursor.hide()
            return
        point = self.master_view.mapSceneToView(position)
        self.cursor.setPos(point.x())
        self.cursor.show()
        self.cursor_moved.emit(float(point.x()))
