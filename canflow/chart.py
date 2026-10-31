from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from .core import SignalKey


PALETTE = (
    "#38bdf8", "#fb7185", "#a3e635", "#fbbf24", "#c084fc", "#2dd4bf",
    "#f97316", "#60a5fa", "#e879f9", "#4ade80", "#facc15", "#f472b6",
)
LIGHT_PALETTE = (
    "#1679a5", "#b94359", "#537c15", "#956b0c", "#8950b1", "#168678",
    "#b65b20", "#3176bc", "#a33daf", "#278652", "#8d7410", "#b6457a",
)


def signal_color(key: SignalKey, theme: str = "浅色") -> str:
    digest = hashlib.sha256(key.storage_key().encode("utf-8")).digest()
    palette = PALETTE if theme == "深色" else LIGHT_PALETTE
    return palette[int.from_bytes(digest[:2], "big") % len(palette)]


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


class CaptureTimeAxis(pg.AxisItem):
    """Keep precise relative X coordinates while formatting capture-clock ticks."""

    def __init__(self):
        super().__init__(orientation="bottom")
        self.mode = "relative"
        self.origin: float | None = None

    def tickStrings(self, values, scale, spacing):  # noqa: N802 - pyqtgraph API
        if self.mode != "capture" or self.origin is None:
            return super().tickStrings(values, scale, spacing)
        moments = [datetime.fromtimestamp(self.origin + value) for value in values]
        origin_date = datetime.fromtimestamp(self.origin).date()
        multiple_dates = bool(moments) and (
            any(moment.date() != moments[0].date() for moment in moments)
            or moments[0].date() != origin_date
        )
        pattern = "%Y-%m-%d %H:%M:%S" if multiple_dates else "%H:%M:%S"
        return [moment.strftime(pattern) for moment in moments]

    def refresh_label(self) -> None:
        if self.mode == "capture":
            date = datetime.fromtimestamp(self.origin).strftime("%Y-%m-%d") if self.origin is not None else ""
            self.setLabel(text=f"原始采集时间（首帧 {date}）")
        else:
            self.setLabel(text="相对首帧时间", units="s")
        self.update()


class TrackAxisPanel(QtWidgets.QWidget):
    """Paint one compact Y scale per vertically stacked signal track."""

    def __init__(self, chart: MultiSignalChart | None = None):
        super().__init__()
        self.chart = chart
        self.dark = True
        self.setFixedWidth(180)

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
        background = QtGui.QColor("#101e30" if self.dark else "#ffffff")
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
            axis_x = self.width() - 5
            painter.setPen(QtGui.QPen(color, 1))
            painter.drawLine(axis_x, top, axis_x, bottom)
            low, high = layer.view.viewRange()[1]
            lane_height = bottom - top
            ticks = 3 if lane_height >= 48 else 2
            tick_top = top + min(12, lane_height / 4)
            tick_bottom = bottom - min(12, lane_height / 4)
            for index in range(ticks):
                ratio = index / max(1, ticks - 1)
                y = round(tick_bottom - ratio * (tick_bottom - tick_top))
                value = low + (bottom - y) / lane_height * (high - low)
                painter.setPen(QtGui.QPen(color, 1))
                painter.drawLine(axis_x - 5, y, axis_x, y)
                painter.setPen(foreground)
                painter.drawText(QtCore.QRect(self.width() - 62, y - 9, 48, 18),
                                 QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
                                 self._number(value))
            painter.save()
            font = painter.font()
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(color)
            label_width = max(24, self.width() - 68)
            title = painter.fontMetrics().elidedText(
                layer.key.name, QtCore.Qt.TextElideMode.ElideRight, label_width
            )
            center = (top + bottom) // 2
            painter.drawText(QtCore.QRect(8, center - 28, label_width, 22),
                             QtCore.Qt.AlignmentFlag.AlignVCenter, title)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(foreground)
            details = f"CAN {key.channel} · 0x{key.frame_id:X}"
            painter.drawText(QtCore.QRect(8, center - 3, label_width, 20),
                             QtCore.Qt.AlignmentFlag.AlignVCenter, details)
            if layer.unit:
                painter.setPen(color)
                painter.drawText(QtCore.QRect(8, center + 20, label_width, 20),
                                 QtCore.Qt.AlignmentFlag.AlignVCenter, f"[{layer.unit}]")
            painter.restore()


@dataclass
class CurveLayer:
    key: SignalKey
    label: str
    unit: str
    color: str
    view: pg.ViewBox
    curve: pg.PlotDataItem
    samples: pg.ScatterPlotItem
    visible: bool = True
    range_initialized: bool = False
    custom_color: str | None = None


class MultiSignalChart(QtWidgets.QWidget):
    """One capture timeline with an independent ViewBox for every signal."""

    cursor_moved = QtCore.Signal(float)
    measurement_clicked = QtCore.Signal(float)
    ranges_changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layers: dict[SignalKey, CurveLayer] = {}
        self.focused: SignalKey | None = None
        self.auto_scale = True
        self.y_locked = False
        self.sample_emphasis = 2.5
        self.theme = "深色"
        self.master_view = TimelineViewBox(self.scale_visible_y)
        self.time_axis = CaptureTimeAxis()
        self.widget = pg.PlotWidget(viewBox=self.master_view, axisItems={"bottom": self.time_axis},
                                    background="#101e30")
        self.widget.showGrid(x=True, y=True, alpha=0.2)
        self.time_axis.refresh_label()
        # Keep the grid axis alive without duplicating the per-track Y scales.
        self.widget.getPlotItem().getAxis("left").setStyle(showValues=False, tickLength=0)
        self.widget.getPlotItem().getAxis("left").setWidth(0)
        self.widget.getPlotItem().hideButtons()
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.axis_panel = TrackAxisPanel(self)
        self.axis_panel.hide()
        layout.addWidget(self.axis_panel)
        layout.addWidget(self.widget, 1)
        self.cursor = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#94a3b8", width=1))
        self.cursor.hide()
        self.master_view.addItem(self.cursor, ignoreBounds=True)
        self.marker_a = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#fbbf24", width=2),
                                        label="A", labelOpts={"position": 0.95})
        self.marker_b = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#fb7185", width=2),
                                        label="B", labelOpts={"position": 0.95})
        for marker in (self.marker_a, self.marker_b):
            marker.hide()
            self.master_view.addItem(marker, ignoreBounds=True)
        self.measurement_enabled = False
        self.widget.scene().sigMouseClicked.connect(self._mouse_clicked)
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
        effective_color = color or signal_color(key, self.theme)
        curve = pg.PlotDataItem([], [], pen=pg.mkPen(effective_color, width=1),
                                autoDownsample=True, clipToView=True)
        view.addItem(curve)
        samples = pg.ScatterPlotItem([], [], symbol="o", size=self._sample_size(),
                                     pen=None, brush=pg.mkBrush(effective_color))
        view.addItem(samples)
        layer = CurveLayer(key, label, unit, effective_color, view, curve, samples, custom_color=color)
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
        layer.view.removeItem(layer.samples)
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
        for layer in self.layers.values():
            layer.view.setBorder(None)
        self.axis_panel.update()

    def set_capture_origin(self, timestamp: float | None) -> None:
        self.time_axis.origin = timestamp
        self.time_axis.refresh_label()

    def set_time_mode(self, mode: str) -> None:
        if mode not in {"relative", "capture"}:
            raise ValueError(mode)
        self.time_axis.mode = mode
        self.time_axis.refresh_label()

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

    def set_data(self, key: SignalKey, x: list[float], y: list[float],
                 sample_x: list[float] | None = None, sample_y: list[float] | None = None) -> None:
        layer = self.layers.get(key)
        if layer is None:
            return
        layer.curve.setData(x, y)
        layer.samples.setData(x if sample_x is None else sample_x,
                              y if sample_y is None else sample_y)
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

    def set_sample_emphasis(self, width: float) -> None:
        self.sample_emphasis = width
        for layer in self.layers.values():
            layer.samples.setSize(self._sample_size())

    def _sample_size(self) -> float:
        return 2 + 2 * self.sample_emphasis

    def set_measure_markers(self, a: float | None, b: float | None) -> None:
        for marker, position in ((self.marker_a, a), (self.marker_b, b)):
            if position is None:
                marker.hide()
            else:
                marker.setPos(position)
                marker.show()

    def _mouse_clicked(self, event) -> None:
        if not self.measurement_enabled or event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        position = event.scenePos()
        if not self.master_view.sceneBoundingRect().contains(position):
            return
        self.measurement_clicked.emit(float(self.master_view.mapSceneToView(position).x()))
        event.accept()

    def set_grid(self, visible: bool) -> None:
        self.widget.showGrid(x=visible, y=visible, alpha=0.2)

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        background = "#101e30" if theme == "深色" else "#ffffff"
        foreground = "#d1d5db" if theme == "深色" else "#1f2937"
        self.widget.setBackground(background)
        for axis_name in ("left", "bottom"):
            self.widget.getPlotItem().getAxis(axis_name).setTextPen(foreground)
        for layer in self.layers.values():
            layer.color = layer.custom_color or signal_color(layer.key, theme)
            layer.curve.setPen(pg.mkPen(layer.color, width=1))
            layer.samples.setBrush(pg.mkBrush(layer.color))
        for marker, light, dark in ((self.marker_a, "#a86b12", "#fbbf24"),
                                    (self.marker_b, "#b94359", "#fb7185")):
            color = dark if theme == "深色" else light
            marker.setPen(pg.mkPen(color, width=1))
            marker.label.setColor(color)
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
        self.axis_panel.setVisible(bool(visible_layers))
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
