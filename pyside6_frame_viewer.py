from __future__ import annotations

import ctypes
import io
import json
import sys
import zipfile
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from datetime import datetime
from math import ceil, floor
from pathlib import Path
from time import perf_counter
from typing import Callable
from uuid import uuid4

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QMimeData, QPoint, QRect, QSettings, Qt, QTimer, QSize, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QIcon, QImage, QKeyEvent, QMouseEvent, QPainter, QPen, QPixmap, QPolygon
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QSlider,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from canvas_view import BlankCanvasView, CanvasView
from gif_loader import export_png_sequence, load_gif_frames
from history import FrameDrawingHistory, HistoryMemoryManager, DrawingOperation


APP_DIR = Path(__file__).resolve().parent
DEFAULT_FRAME_DIR = APP_DIR / "work" / "demo_frames"
PROJECT_SUFFIX = ".giftrace"
TIMELINE_LABEL_WIDTH = 156


@dataclass
class FrameData:
    """All state that belongs to one timeline frame, identified independently of its position."""

    frame_id: str
    path: Path
    duration: int = 120
    exposure: int = 1
    reference_image: QImage | None = None
    drawing: QImage | None = None
    history: FrameDrawingHistory | None = None
    thumbnail_reference: QImage | None = None
    pixmap: QPixmap | None = None
    thumbnail_cache: dict[str, QPixmap] = field(default_factory=dict)
    thumbnail_dirty: bool = True


@dataclass
class LayerGroup:
    """One independently editable animation layer, keyed by stable frame ids."""

    group_id: str
    name: str
    visible: bool = True
    drawings: dict[str, QImage] = field(default_factory=dict)
    histories: dict[str, FrameDrawingHistory] = field(default_factory=dict)


@dataclass
class TimelineGeometry:
    """The single frame coordinate system shared by every timeline surface."""

    zoom: float = 1.0
    scroll_x: int = 0
    base_frame_cell_width: int = 38
    base_track_row_height: int = 36

    def set_zoom_percent(self, percent: int) -> None:
        self.zoom = max(0.5, min(2.0, int(percent) / 100.0))

    def frame_width(self) -> int:
        return max(20, round(self.base_frame_cell_width * self.zoom))

    def row_height(self) -> int:
        return max(28, min(48, round(self.base_track_row_height * self.zoom)))

    def frame_left(self, index: int) -> int:
        return int(index) * self.frame_width() - int(self.scroll_x)

    def frame_center(self, index: int) -> float:
        return self.frame_left(index) + self.frame_width() / 2.0

    def frame_right(self, index: int) -> int:
        return self.frame_left(index) + self.frame_width()

    def frame_index_at(self, x: float, frame_count: int | None = None) -> int:
        index = floor((float(x) + self.scroll_x) / self.frame_width())
        if frame_count is None:
            return max(0, index)
        return max(0, min(index, max(0, frame_count - 1)))

    def insert_index_at(self, x: float, frame_count: int) -> int:
        index = floor((float(x) + self.scroll_x + self.frame_width() / 2.0) / self.frame_width())
        return max(0, min(index, max(0, frame_count)))

    def visible_frame_range(self, viewport_width: int, frame_count: int | None = None) -> tuple[int, int]:
        if viewport_width <= 0 or frame_count == 0:
            return 0, -1
        first = max(0, floor(self.scroll_x / self.frame_width()))
        last = max(first, ceil((self.scroll_x + viewport_width) / self.frame_width()) - 1)
        if frame_count is not None:
            last = min(last, frame_count - 1)
            if first >= frame_count:
                return frame_count, frame_count - 1
        return first, last

    def content_width(self, frame_count: int) -> int:
        return max(0, int(frame_count)) * self.frame_width()

    def thumbnail_size(self) -> QSize:
        return QSize(max(16, self.frame_width() - 4), max(20, self.row_height() - 4))


class FrameFieldMapping(MutableMapping[int, object]):
    """Compatibility view over one FrameData field; it never owns a second copy of state."""

    def __init__(self, owner: "FrameViewerWindow", field_name: str) -> None:
        self._owner = owner
        self._field_name = field_name

    def __getitem__(self, key: int):
        value = getattr(self._owner.frames[key], self._field_name)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: int, value) -> None:
        setattr(self._owner.frames[key], self._field_name, value)

    def __delitem__(self, key: int) -> None:
        setattr(self._owner.frames[key], self._field_name, None)

    def __iter__(self):
        return (index for index, frame in enumerate(self._owner.frames) if getattr(frame, self._field_name) is not None)

    def __len__(self) -> int:
        return sum(getattr(frame, self._field_name) is not None for frame in self._owner.frames)

    def clear(self) -> None:
        for frame in self._owner.frames:
            setattr(frame, self._field_name, None)


def ensure_demo_png_sequence(frame_dir: Path, count: int = 12) -> list[Path]:
    """Create a small local PNG sequence when no frames are available."""

    frame_dir.mkdir(parents=True, exist_ok=True)
    existing_paths = sorted(frame_dir.glob("*.png"))
    if existing_paths:
        return existing_paths

    colors = [
        QColor("#3b82f6"),
        QColor("#10b981"),
        QColor("#f59e0b"),
        QColor("#ef4444"),
        QColor("#8b5cf6"),
        QColor("#06b6d4"),
    ]

    for index in range(count):
        image = QImage(640, 360, QImage.Format.Format_ARGB32)
        image.fill(colors[index % len(colors)])

        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 42))
        painter.drawEllipse(70 + index * 18, 70, 160, 160)
        painter.drawRoundedRect(340 - index * 9, 120, 210, 110, 18, 18)
        painter.setPen(QPen(QColor("white"), 4))
        painter.drawLine(60, 290 - index * 8, 580, 95 + index * 5)
        painter.setFont(QFont("Arial", 46, QFont.Weight.Bold))
        painter.setPen(QColor("white"))
        painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, f"Frame {index:02d}")
        painter.setFont(QFont("Arial", 16))
        painter.drawText(24, 330, "Local PNG sequence demo")
        painter.end()

        image.save(str(frame_dir / f"frame_{index:04d}.png"))

    return sorted(frame_dir.glob("*.png"))


def disable_windows_pointer_feedback(window: QWidget) -> None:
    """Turn off Windows touch/pen press ripples for this app window only."""
    if sys.platform != "win32":
        return

    try:
        set_feedback = ctypes.windll.user32.SetWindowFeedbackSetting
        set_feedback.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_void_p,
        ]
        set_feedback.restype = ctypes.c_int
        disabled = ctypes.c_int(0)
        for feedback_type in range(1, 12):
            set_feedback(
                ctypes.c_void_p(int(window.winId())),
                feedback_type,
                0,
                ctypes.sizeof(disabled),
                ctypes.byref(disabled),
            )
    except (AttributeError, OSError):
        # The API is unavailable on older Windows versions.
        pass


class ReferenceFloatWindow(QWidget):
    """A draggable, resizable reference CanvasView constrained to its host."""

    _RESIZE_MARGIN = 8

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(160, 90)
        self.resize(360, 240)
        self.setStyleSheet("background: #1f2937;")

        self.canvas = CanvasView(self)
        self.canvas.set_tracing_visible(False)
        self.canvas.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas)

        self._drag_origin: QPoint | None = None
        self._initial_geometry = self.geometry()
        self._resize_edges: set[str] = set()

    def set_host(self, host: QWidget) -> None:
        self.setParent(host)
        host.installEventFilter(self)
        self._keep_within_host()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        QTimer.singleShot(0, self.canvas.fit_to_view)
        super().resizeEvent(event)

    def eventFilter(self, watched, event):  # type: ignore[override]
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._keep_within_host)
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._drag_origin = event.globalPosition().toPoint()
        self._initial_geometry = self.geometry()
        self._resize_edges = self._edges_at(event.position().toPoint())
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._drag_origin is None:
            self.setCursor(self._cursor_for_edges(self._edges_at(event.position().toPoint())))
            super().mouseMoveEvent(event)
            return

        delta = event.globalPosition().toPoint() - self._drag_origin
        if not self._resize_edges:
            self.move(self._constrained_top_left(self._initial_geometry.topLeft() + delta))
        else:
            geometry = self._resized_geometry(delta)
            self.setGeometry(geometry)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = None
            self._resize_edges.clear()
            self.setCursor(self._cursor_for_edges(self._edges_at(event.position().toPoint())))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _edges_at(self, position: QPoint) -> set[str]:
        edges: set[str] = set()
        margin = self._RESIZE_MARGIN
        if position.x() <= margin:
            edges.add("left")
        elif position.x() >= self.width() - margin:
            edges.add("right")
        if position.y() <= margin:
            edges.add("top")
        elif position.y() >= self.height() - margin:
            edges.add("bottom")
        return edges

    def _resized_geometry(self, delta: QPoint) -> QRect:
        geometry = self._initial_geometry
        left = geometry.left()
        top = geometry.top()
        right = geometry.right()
        bottom = geometry.bottom()
        if "left" in self._resize_edges:
            left += delta.x()
        if "right" in self._resize_edges:
            right += delta.x()
        if "top" in self._resize_edges:
            top += delta.y()
        if "bottom" in self._resize_edges:
            bottom += delta.y()

        minimum_width = self.minimumWidth()
        minimum_height = self.minimumHeight()
        if right - left + 1 < minimum_width:
            if "left" in self._resize_edges:
                left = right - minimum_width + 1
            else:
                right = left + minimum_width - 1
        if bottom - top + 1 < minimum_height:
            if "top" in self._resize_edges:
                top = bottom - minimum_height + 1
            else:
                bottom = top + minimum_height - 1
        geometry = QRect(left, top, right - left + 1, bottom - top + 1)
        return self._constrained_geometry(geometry)

    def _keep_within_host(self) -> None:
        self.setGeometry(self._constrained_geometry(self.geometry()))

    def _constrained_top_left(self, point: QPoint) -> QPoint:
        return self._constrained_geometry(QRect(point, self.size())).topLeft()

    def _constrained_geometry(self, geometry: QRect) -> QRect:
        host = self.parentWidget()
        if host is None or host.width() <= 0 or host.height() <= 0:
            return geometry
        bounds = host.rect()
        width = min(geometry.width(), bounds.width())
        height = min(geometry.height(), bounds.height())
        max_x = max(bounds.left(), bounds.right() - width + 1)
        max_y = max(bounds.top(), bounds.bottom() - height + 1)
        left = min(max(geometry.left(), bounds.left()), max_x)
        top = min(max(geometry.top(), bounds.top()), max_y)
        return QRect(left, top, width, height)

    @staticmethod
    def _cursor_for_edges(edges: set[str]) -> Qt.CursorShape:
        if ("left" in edges and "top" in edges) or ("right" in edges and "bottom" in edges):
            return Qt.CursorShape.SizeFDiagCursor
        if ("right" in edges and "top" in edges) or ("left" in edges and "bottom" in edges):
            return Qt.CursorShape.SizeBDiagCursor
        if "left" in edges or "right" in edges:
            return Qt.CursorShape.SizeHorCursor
        if "top" in edges or "bottom" in edges:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor


class TimelineListWidget(QListWidget):
    """One track viewport using the shared geometry and stable frame-id drag path."""

    FRAME_MIME_TYPE = "application/x-gif-trace-frame-id"

    drag_started = Signal()
    frame_move_requested = Signal(str, str, bool)
    drag_finished = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._selected_frame_id_before_drag = ""
        self._insert_x: int | None = None
        self._pending_move: tuple[str, str, bool] | None = None
        self._geometry: TimelineGeometry | None = None
        self._current_index_provider: Callable[[], int] | None = None
        self._vertical_wheel_handler: Callable[[object], bool] | None = None

    def set_vertical_wheel_handler(self, handler: Callable[[object], bool]) -> None:
        self._vertical_wheel_handler = handler

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        # QListWidget starts changing its hidden horizontal scrollbar once wide
        # zoom levels create real overflow.  Never let a track own wheel scroll;
        # forward it to the one vertical timeline container instead.
        if self._vertical_wheel_handler is not None:
            self._vertical_wheel_handler(event)
        else:
            event.accept()

    def set_timeline_geometry(
        self,
        geometry: TimelineGeometry,
        current_index_provider: Callable[[], int],
    ) -> None:
        self._geometry = geometry
        self._current_index_provider = current_index_provider
        self.viewport().update()

    def startDrag(self, supported_actions) -> None:  # type: ignore[override]
        current = self.currentItem()
        self._selected_frame_id_before_drag = str(current.data(Qt.ItemDataRole.UserRole)) if current is not None else ""
        if not self._selected_frame_id_before_drag:
            return
        self._pending_move = None
        self.drag_started.emit()
        try:
            mime_data = QMimeData()
            mime_data.setData(self.FRAME_MIME_TYPE, self._selected_frame_id_before_drag.encode("utf-8"))
            drag = QDrag(self)
            drag.setMimeData(mime_data)
            drag.setPixmap(current.icon().pixmap(self.iconSize()))
            drag.exec(Qt.DropAction.MoveAction, Qt.DropAction.MoveAction)
        finally:
            pending_move = self._pending_move
            self._pending_move = None
            self._selected_frame_id_before_drag = ""
            self._set_insert_x(None)
            self.drag_finished.emit()
        if pending_move is not None:
            self.frame_move_requested.emit(*pending_move)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        source_id = self._frame_id_from_event(event)
        if not source_id or source_id != self._selected_frame_id_before_drag or self._item_for_frame_id(source_id) is None:
            self._set_insert_x(None)
            event.ignore()
            return
        position = event.position().toPoint()
        anchor_id, place_after = self._anchor_for_position(position)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        if anchor_id != source_id:
            self._pending_move = (source_id, anchor_id, place_after)
        self._set_insert_x(None)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if self._frame_id_from_event(event) == self._selected_frame_id_before_drag:
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._frame_id_from_event(event) != self._selected_frame_id_before_drag:
            self._set_insert_x(None)
            event.ignore()
            return
        self._set_insert_x(self._insert_x_for_position(event.position().toPoint()))
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def dragLeaveEvent(self, event) -> None:  # type: ignore[override]
        self._set_insert_x(None)
        event.accept()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        geometry = self._geometry
        if geometry is not None:
            first, last = geometry.visible_frame_range(self.viewport().width())
            painter.setPen(QPen(QColor("#343a43"), 1))
            for index in range(first, last + 2):
                x = geometry.frame_right(index) - 1
                painter.drawLine(x, 0, x, self.viewport().height())
            painter.setPen(QPen(QColor("#303640"), 1))
            painter.drawLine(0, self.viewport().height() - 1, self.viewport().width(), self.viewport().height() - 1)
        if geometry is not None and self._current_index_provider is not None and self.count():
            current = max(0, min(self._current_index_provider(), self.count() - 1))
            playhead_x = round(geometry.frame_center(current))
            painter.setPen(QPen(QColor("#3b82f6"), 2))
            painter.drawLine(playhead_x, 0, playhead_x, self.viewport().height())
        if self._insert_x is not None:
            color = QColor("#a5f3fc")
            painter.setPen(QPen(color, 2))
            painter.drawLine(self._insert_x, 5, self._insert_x, self.viewport().height() - 5)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawPolygon(QPolygon([QPoint(self._insert_x - 4, 0), QPoint(self._insert_x + 4, 0), QPoint(self._insert_x, 5)]))
            bottom = self.viewport().height() - 1
            painter.drawPolygon(
                QPolygon([QPoint(self._insert_x - 4, bottom), QPoint(self._insert_x + 4, bottom), QPoint(self._insert_x, bottom - 5)])
            )
        painter.end()

    def _insert_x_for_position(self, position: QPoint) -> int:
        if self._geometry is not None:
            insert_index = self._geometry.insert_index_at(position.x(), self.count())
            return self._geometry.frame_left(insert_index)
        anchor_id, place_after = self._anchor_for_position(position)
        item = self._item_for_frame_id(anchor_id) if anchor_id else None
        if item is None:
            return 4 if self.count() == 0 else self.visualItemRect(self.item(self.count() - 1)).right() + 4
        rect = self.visualItemRect(item)
        return rect.right() + 1 if place_after else rect.left()

    def _anchor_for_position(self, position: QPoint) -> tuple[str, bool]:
        if self._geometry is not None:
            content_x = position.x() + self._geometry.scroll_x
            if content_x >= self._geometry.content_width(self.count()):
                return "", True
            index = self._geometry.frame_index_at(position.x(), self.count())
            item = self.item(index)
            center_pixel = self._geometry.frame_left(index) + (self._geometry.frame_width() - 1) // 2
            return (
                str(item.data(Qt.ItemDataRole.UserRole)),
                position.x() >= center_pixel,
            )
        target_item = self.itemAt(position)
        if target_item is not None:
            target_rect = self.visualItemRect(target_item)
            return (
                str(target_item.data(Qt.ItemDataRole.UserRole)),
                position.x() >= target_rect.center().x(),
            )
        for index in range(self.count()):
            item = self.item(index)
            if position.x() < self.visualItemRect(item).center().x():
                return str(item.data(Qt.ItemDataRole.UserRole)), False
        return "", True

    def _set_insert_x(self, value: int | None) -> None:
        if self._insert_x == value:
            return
        self._insert_x = value
        self.viewport().update()

    def _item_for_frame_id(self, frame_id: str) -> QListWidgetItem | None:
        for index in range(self.count()):
            item = self.item(index)
            if str(item.data(Qt.ItemDataRole.UserRole)) == frame_id:
                return item
        return None

    def _frame_id_from_event(self, event) -> str:
        if event.source() is not self or not event.mimeData().hasFormat(self.FRAME_MIME_TYPE):
            return ""
        return bytes(event.mimeData().data(self.FRAME_MIME_TYPE)).decode("utf-8", errors="ignore")


class TimelineFrameDelegate(QStyledItemDelegate):
    """Paint previews inside frame cells; the shared geometry owns all layout."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:  # type: ignore[override]
        painter.save()
        view = option.widget
        geometry = getattr(view, "_geometry", None)
        if not isinstance(geometry, TimelineGeometry):
            geometry = TimelineGeometry()
        rect = QRect(geometry.frame_left(index.row()), 0, geometry.frame_width(), geometry.row_height())
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.fillRect(rect, QColor("#242830"))
        current_provider = getattr(view, "_current_index_provider", None)
        is_current = current_provider is not None and index.row() == current_provider()
        if is_current:
            painter.fillRect(rect, QColor(49, 104, 176, 34))
        painter.setPen(QPen(QColor("#343a43"), 1))
        painter.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(icon, QIcon) and not icon.isNull():
            icon_size = geometry.thumbnail_size()
            pixmap = icon.pixmap(icon_size)
            icon_rect = QRect(
                rect.center().x() - icon_size.width() // 2,
                rect.center().y() - icon_size.height() // 2,
                icon_size.width(),
                icon_size.height(),
            )
            painter.fillRect(icon_rect, QColor("#303640"))
            painter.drawPixmap(icon_rect, pixmap)
            painter.setPen(QPen(QColor("#414852"), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(icon_rect.adjusted(0, 0, -1, -1))
        if selected and bool(view.property("activeTimelineTrack")):
            painter.setPen(QPen(QColor(86, 156, 246, 190), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
        painter.restore()


class TimelinePaperTrack(QWidget):
    """Compact non-frame paper row using the same global frame grid."""

    def __init__(self, owner: "FrameViewerWindow") -> None:
        super().__init__()
        self._owner = owner

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#242830"))
        geometry = self._owner.timeline_geometry
        first, last = geometry.visible_frame_range(self.width())
        current = self._owner.current_index
        if first <= current <= last + 1:
            painter.fillRect(
                QRect(geometry.frame_left(current), 0, geometry.frame_width(), self.height()),
                QColor(49, 104, 176, 26),
            )
        painter.setPen(QPen(QColor("#343a43"), 1))
        for index in range(first, last + 2):
            x = geometry.frame_right(index) - 1
            painter.drawLine(x, 0, x, self.height())
        if self._owner.frames:
            playhead_x = round(geometry.frame_center(current))
            painter.setPen(QPen(QColor("#3b82f6"), 2))
            painter.drawLine(playhead_x, 0, playhead_x, self.height())
        painter.setPen(QPen(QColor("#303640"), 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        painter.end()


class TimelineRuler(QWidget):
    """Compact ruler, playback range and top segment of the global playhead."""

    def __init__(self, owner: "FrameViewerWindow") -> None:
        super().__init__(owner)
        self._owner = owner
        self._timeline: TimelineListWidget | None = None
        self._drag_handle: str | None = None
        self.setFixedHeight(20)
        self.setMinimumWidth(1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点击序号切换帧")

    def set_timeline(self, timeline: TimelineListWidget | None) -> None:
        self._timeline = timeline
        self.update()

    def _scroll_changed(self, _value: int) -> None:
        self.update()

    def _sequence_geometry(self) -> tuple[float, int]:
        geometry = self._owner.timeline_geometry
        return geometry.frame_center(0), geometry.frame_width()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#242830"))
        painter.setPen(QPen(QColor("#353b45"), 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        geometry = self._owner.timeline_geometry
        first, last = geometry.visible_frame_range(self.width())
        font = painter.font()
        font.setPixelSize(9)
        font.setBold(False)
        painter.setFont(font)
        label_step = max(1, ceil(96 / geometry.frame_width()))
        for index in range(first, last + 2):
            left = geometry.frame_left(index)
            painter.setPen(QPen(QColor("#454c57"), 1))
            painter.drawLine(left, self.height() - 6, left, self.height() - 1)
            if index % label_step == 0:
                painter.setPen(QColor("#aeb7c4"))
                painter.drawText(QRect(left + 3, 1, geometry.frame_width() - 6, 12), Qt.AlignmentFlag.AlignLeft, str(index + 1))

        range_start_x = geometry.frame_left(self._owner.playback_range_start)
        range_end_x = geometry.frame_right(self._owner.playback_range_end)
        painter.fillRect(QRect(range_start_x, self.height() - 2, max(1, range_end_x - range_start_x), 2), QColor("#b7791f"))
        for x in (range_start_x, range_end_x):
            painter.setPen(QPen(QColor("#f59e0b"), 1))
            painter.drawLine(x, self.height() - 6, x, self.height() - 1)
            painter.fillRect(QRect(x - 2, self.height() - 6, 5, 2), QColor("#f59e0b"))

        if self._owner.frames:
            playhead_x = round(geometry.frame_center(self._owner.current_index))
            painter.setPen(QPen(QColor("#3b82f6"), 2))
            painter.drawLine(playhead_x, 0, playhead_x, self.height())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#3b82f6"))
            painter.drawPolygon(QPolygon([QPoint(playhead_x - 4, 0), QPoint(playhead_x + 4, 0), QPoint(playhead_x, 5)]))
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        geometry = self._owner.timeline_geometry
        start_x = geometry.frame_left(self._owner.playback_range_start)
        end_x = geometry.frame_right(self._owner.playback_range_end)
        position_x = event.position().x()
        if abs(position_x - start_x) <= 9 or abs(position_x - end_x) <= 9:
            self._drag_handle = "start" if abs(position_x - start_x) <= abs(position_x - end_x) else "end"
            self._move_range_handle(position_x)
            event.accept()
            return
        index = geometry.frame_index_at(event.position().x(), len(self._owner.frames))
        if 0 <= index < len(self._owner.frames):
            self._owner._request_timeline_frame(self._owner.active_layer_group_id, index)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._drag_handle is not None:
            self._move_range_handle(event.position().x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._drag_handle is not None:
            self._move_range_handle(event.position().x())
            self._drag_handle = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _move_range_handle(self, position_x: float) -> None:
        if not self._owner.frames or self._drag_handle is None:
            return
        geometry = self._owner.timeline_geometry
        boundary = round((position_x + geometry.scroll_x) / geometry.frame_width())
        if self._drag_handle == "start":
            index = max(0, min(len(self._owner.frames) - 1, boundary))
            self._owner.set_playback_range(min(index, self._owner.playback_range_end), self._owner.playback_range_end)
        else:
            index = max(0, min(len(self._owner.frames) - 1, boundary - 1))
            self._owner.set_playback_range(self._owner.playback_range_start, max(index, self._owner.playback_range_start))

class FrameViewerWindow(QMainWindow):
    def __init__(self, frame_dir: Path = DEFAULT_FRAME_DIR) -> None:
        super().__init__()
        self.setWindowTitle("GIF Reference Tracing Viewer")
        self.resize(1280, 820)

        self.frames = [
            FrameData(frame_id=uuid4().hex, path=path)
            for path in ensure_demo_png_sequence(frame_dir)
        ]
        self.current_gif_path: Path | None = None
        self.current_project_path: Path | None = None
        self.current_index = 0
        self.playback_range_start = 0
        self.playback_range_end = max(0, len(self.frames) - 1)
        self.practice_scale = 3
        self.current_drawing_size = QSize(1, 1)
        self.drawing_layers = FrameFieldMapping(self, "drawing")
        self.frame_histories = FrameFieldMapping(self, "history")
        self.layer_groups = [LayerGroup(uuid4().hex, "图层组 1")]
        self.active_layer_group_id = self.layer_groups[0].group_id
        self.paper_visible = True
        self._updating_layer_list = False
        self._history_memory = HistoryMemoryManager()
        self._layer_stroke_clipboard: tuple[str, QImage] | None = None
        self._pending_stroke_before: dict[str, tuple[str, QImage]] = {}
        self._drawing_operation_context: dict[object, tuple[str, str]] = {}
        self._syncing_drawing = False
        self._settings = QSettings("GIF Reference Tracing Viewer", "GIF Reference Tracing Viewer")
        self.thumbnail_display_mode = str(self._settings.value("thumbnail_display_mode", "drawing"))
        self._thumbnail_cache: dict[tuple[object, ...], QPixmap] = {}
        self._group_thumbnail_cache: dict[tuple[object, ...], QPixmap] = {}
        self._thumbnail_content_versions: dict[tuple[str, str], int] = {}
        self._thumbnail_dirty: set[str] = set()
        self._thumbnail_generation = 0
        self._thumbnail_reference_cache = FrameFieldMapping(self, "thumbnail_reference")
        self._reference_images_by_index = FrameFieldMapping(self, "reference_image")
        self._frame_pixmap_cache = FrameFieldMapping(self, "pixmap")
        self._last_frame_switch_at = 0.0
        self._pending_timeline_frame_id: str | None = None
        self.playback_fps = max(1, min(60, int(self._settings.value("playback_fps", 12))))

        self.play_timer = QTimer(self)
        self.play_timer.setInterval(self._playback_interval())
        self.play_timer.timeout.connect(self.advance_playback)
        self._playback_exposure_tick = 0
        self._frame_switch_timer = QTimer(self)
        self._frame_switch_timer.setSingleShot(True)
        self._frame_switch_timer.timeout.connect(self._apply_pending_timeline_frame)
        self._frame_preload_timer = QTimer(self)
        self._frame_preload_timer.setSingleShot(True)
        self._frame_preload_timer.timeout.connect(self._preload_adjacent_frames)
        self._pending_view_mode: int | None = None
        self._view_switch_timer = QTimer(self)
        self._view_switch_timer.setSingleShot(True)
        self._view_switch_timer.timeout.connect(self._apply_pending_view_mode)

        self.overlay_canvas = CanvasView()
        self.compare_reference_canvas = CanvasView()
        self.reference_only_canvas = CanvasView()
        self.compare_trace_canvas = BlankCanvasView()
        self.trace_only_canvas = BlankCanvasView()
        self.float_trace_canvas = BlankCanvasView()
        self.reference_float_window = ReferenceFloatWindow()
        self.reference_float_canvas = self.reference_float_window.canvas
        self.reference_canvases = [
            self.overlay_canvas,
            self.compare_reference_canvas,
            self.reference_only_canvas,
            self.reference_float_canvas,
        ]
        self.all_canvases = [
            self.overlay_canvas,
            self.compare_reference_canvas,
            self.reference_only_canvas,
            self.compare_trace_canvas,
            self.trace_only_canvas,
            self.float_trace_canvas,
            self.reference_float_canvas,
        ]
        self.drawing_canvases = [
            self.overlay_canvas,
            self.compare_trace_canvas,
            self.trace_only_canvas,
            self.float_trace_canvas,
        ]
        for canvas in self.drawing_canvases:
            canvas.set_drawing_enabled(True)
            canvas.drawing_changed.connect(self.update_current_drawing_layer)
            canvas.stroke_started.connect(self.begin_drawing_operation)
            canvas.drawing_attempted.connect(self.pause_playback_for_drawing)
        self.compare_reference_canvas.set_tracing_visible(False)
        self.reference_only_canvas.set_tracing_visible(False)

        self._timeline_drag_active = False
        saved_timeline_zoom = max(50, min(200, int(self._settings.value("timeline_zoom", 100))))
        self.timeline_zoom = saved_timeline_zoom / 100.0
        self.timeline_scroll_x = 0
        self.timeline_geometry = TimelineGeometry()
        self.timeline_geometry.set_zoom_percent(saved_timeline_zoom)
        self._syncing_timeline_scroll = False
        self.timeline = TimelineListWidget()
        self._configure_group_timeline(self.timeline)
        self.timeline.currentRowChanged.connect(self._timeline_row_changed)
        self.timeline.drag_started.connect(self._set_timeline_drag_active)
        self.timeline.drag_finished.connect(self._clear_timeline_drag_active)
        self.timeline.frame_move_requested.connect(self._timeline_frame_move_requested)
        self.timeline.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.timeline.customContextMenuRequested.connect(self._show_timeline_context_menu)
        self.timeline.viewport().installEventFilter(self)
        self.group_timeline_widgets: dict[str, TimelineListWidget] = {}
        self.group_timeline_row_hosts: dict[str, QWidget] = {}
        self._primary_timeline_group_id = self.active_layer_group_id
        self._thumbnail_refresh_timer = QTimer(self)
        self._thumbnail_refresh_timer.setSingleShot(True)
        self._thumbnail_refresh_timer.timeout.connect(self._refresh_visible_thumbnails)
        self._pending_thumbnail_display_mode: str | None = None

        self.thumbnail_display_combo = QComboBox()
        self.thumbnail_display_combo.addItem("参考帧", "reference")
        self.thumbnail_display_combo.addItem("绘画层", "drawing")
        self.thumbnail_display_combo.addItem("合成", "composite")
        thumbnail_mode_index = self.thumbnail_display_combo.findData(self.thumbnail_display_mode)
        self.thumbnail_display_combo.setCurrentIndex(
            thumbnail_mode_index if thumbnail_mode_index >= 0 else 2
        )
        self.thumbnail_display_mode = self.thumbnail_display_combo.currentData()
        self.thumbnail_display_combo.currentIndexChanged.connect(self.set_thumbnail_display_mode)

        self.timeline_zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.timeline_zoom_slider.setRange(50, 200)
        self.timeline_zoom_slider.setSingleStep(5)
        self.timeline_zoom_slider.setPageStep(25)
        self.timeline_zoom_slider.setFixedWidth(150)
        self.timeline_zoom_slider.setValue(saved_timeline_zoom)
        self.timeline_zoom_slider.setToolTip("拖动调整时间轴帧缩略图显示倍率")
        self.timeline_zoom_value_label = QLabel(f"{saved_timeline_zoom}%")
        self.timeline_zoom_value_label.setMinimumWidth(42)
        self.timeline_zoom_slider.valueChanged.connect(self.set_timeline_zoom)

        self.import_gif_button = QPushButton("导入 GIF")
        self.import_gif_button.clicked.connect(self.import_gif)

        self.project_button = QToolButton()
        self.project_button.setText("工程")
        self.project_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        project_menu = QMenu(self.project_button)
        project_menu.addAction("打开工程", self.open_project)
        project_menu.addAction("保存工程", self.save_project)
        project_menu.addAction("另存为...", self.save_project_as)
        self.project_button.setMenu(project_menu)

        self.export_gif_button = QPushButton("导出 GIF")
        self.export_gif_button.clicked.connect(self.export_animated_gif)

        self.export_fps_label = QLabel(f"{self.playback_fps} FPS")
        self.export_fps_slider = QSlider(Qt.Orientation.Horizontal)
        self.export_fps_slider.setRange(1, 60)
        self.export_fps_slider.setValue(self.playback_fps)
        self.export_fps_slider.setFixedWidth(92)
        self.export_fps_slider.setToolTip("导出 GIF 帧率")
        self.export_fps_slider.valueChanged.connect(self.set_export_fps)

        self.import_step_label = QLabel("1")
        self.import_step_slider = QSlider(Qt.Orientation.Horizontal)
        self.import_step_slider.setRange(1, 999)
        self.import_step_slider.setValue(1)
        self.import_step_slider.setEnabled(False)
        self.import_step_slider.setFixedWidth(92)
        self.import_step_slider.setPageStep(5)
        self.import_step_slider.setToolTip("导入 GIF 后调整抽帧步数")
        self.import_step_slider.valueChanged.connect(self.set_import_step)

        self.previous_button = QPushButton("上一帧")
        self.previous_button.clicked.connect(self.show_previous_frame)

        self.play_button = QToolButton()
        self.play_button.setCheckable(True)
        self.play_button.setToolTip("Play / pause")
        self.play_button.clicked.connect(self.toggle_playback)

        self.playback_fps_label = QLabel(f"{self.playback_fps} FPS")
        self.playback_fps_slider = QSlider(Qt.Orientation.Horizontal)
        self.playback_fps_slider.setRange(1, 60)
        self.playback_fps_slider.setValue(self.playback_fps)
        self.playback_fps_slider.setFixedWidth(92)
        self.playback_fps_slider.setToolTip("播放速度")
        self.playback_fps_slider.valueChanged.connect(self.set_playback_fps)

        self.next_button = QPushButton("下一帧")
        self.next_button.clicked.connect(self.show_next_frame)

        self.fit_button = QPushButton("适应窗口")
        self.fit_button.clicked.connect(self.fit_current_view)

        self.tool_combo = QComboBox()
        self.tool_combo.addItem("画笔", "brush")
        self.tool_combo.addItem("软边画笔", "soft_brush")
        self.tool_combo.addItem("方形画笔", "square_brush")
        self.tool_combo.addItem("铅笔", "pencil")
        self.tool_combo.addItem("橡皮", "eraser")
        self.tool_combo.addItem("油漆桶", "bucket")
        self.tool_combo.addItem("渐变", "gradient")
        self.tool_combo.currentIndexChanged.connect(self.set_drawing_tool)

        self.square_brush_angle_combo = QComboBox()
        self.square_brush_angle_combo.addItem("固定 45°", False)
        self.square_brush_angle_combo.addItem("轻微沿轨迹旋转", True)
        self.square_brush_angle_combo.setCurrentIndex(1)
        self.square_brush_angle_combo.currentIndexChanged.connect(self.set_square_brush_angle_mode)

        self.brush_color_combo = QComboBox()
        self.brush_color_combo.addItem("黑", "#000000")
        self.brush_color_combo.addItem("红", "#ef4444")
        self.brush_color_combo.addItem("蓝", "#2563eb")
        self.brush_color_combo.addItem("绿", "#16a34a")
        self.brush_color_combo.addItem("白", "#ffffff")
        self.brush_color_combo.currentIndexChanged.connect(self.set_brush_color)

        self.brush_size_combo = QComboBox()
        for size in (2, 4, 8, 12, 20):
            self.brush_size_combo.addItem(f"{size}px", size)
        self.brush_size_combo.setCurrentIndex(1)
        self.brush_size_combo.currentIndexChanged.connect(self.set_brush_size)

        self.brush_opacity_label = QLabel("100%")
        self.brush_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.brush_opacity_slider.setRange(1, 100)
        self.brush_opacity_slider.setValue(100)
        self.brush_opacity_slider.valueChanged.connect(self.set_brush_opacity)

        self.brush_hardness_label = QLabel("20%")
        self.brush_hardness_slider = QSlider(Qt.Orientation.Horizontal)
        self.brush_hardness_slider.setRange(0, 100)
        self.brush_hardness_slider.setValue(20)
        self.brush_hardness_slider.setToolTip("软边画笔硬度")
        self.brush_hardness_slider.valueChanged.connect(self.set_brush_hardness)

        self.soft_renderer_mode_combo = QComboBox()
        self.soft_renderer_mode_combo.addItem("软圆笔", "softRound")
        self.soft_renderer_mode_combo.addItem("喷枪", "airbrush")
        self.soft_renderer_mode_combo.currentIndexChanged.connect(self.set_soft_renderer_mode)

        self.soft_edge_gamma_label = QLabel("0.75")
        self.soft_edge_gamma_slider = QSlider(Qt.Orientation.Horizontal)
        self.soft_edge_gamma_slider.setRange(10, 800)
        self.soft_edge_gamma_slider.setValue(75)
        self.soft_edge_gamma_slider.valueChanged.connect(self.set_soft_edge_gamma_from_slider)

        self.soft_flow_label = QLabel("25%")
        self.soft_flow_slider = QSlider(Qt.Orientation.Horizontal)
        self.soft_flow_slider.setRange(1, 100)
        self.soft_flow_slider.setValue(25)
        self.soft_flow_slider.valueChanged.connect(self.set_soft_flow)

        self.soft_pressure_size_checkbox = QCheckBox("压感控制尺寸")
        self.soft_pressure_size_checkbox.toggled.connect(self.set_soft_pressure_size_enabled)
        self.soft_pressure_size_checkbox.setChecked(True)
        self.soft_pressure_opacity_checkbox = QCheckBox("压感控制覆盖")
        self.soft_pressure_opacity_checkbox.toggled.connect(self.set_soft_pressure_opacity_enabled)

        self.square_pressure_size_checkbox = QCheckBox("压感控制尺寸")
        self.square_pressure_size_checkbox.setChecked(True)
        self.square_pressure_size_checkbox.toggled.connect(self.set_square_pressure_size_enabled)

        self.square_pressure_strength_label = QLabel("22%")
        self.square_pressure_strength_slider = QSlider(Qt.Orientation.Horizontal)
        self.square_pressure_strength_slider.setRange(0, 50)
        self.square_pressure_strength_slider.setValue(22)
        self.square_pressure_strength_slider.valueChanged.connect(self.set_square_pressure_size_strength_from_slider)

        self.square_min_pressure_size_label = QLabel("72%")
        self.square_min_pressure_size_slider = QSlider(Qt.Orientation.Horizontal)
        self.square_min_pressure_size_slider.setRange(30, 100)
        self.square_min_pressure_size_slider.setValue(72)
        self.square_min_pressure_size_slider.valueChanged.connect(self.set_square_min_pressure_size_from_slider)

        self.pencil_grain_label = QLabel("55%")
        self.pencil_grain_slider = QSlider(Qt.Orientation.Horizontal)
        self.pencil_grain_slider.setRange(0, 100)
        self.pencil_grain_slider.setValue(55)
        self.pencil_grain_slider.valueChanged.connect(self.set_pencil_grain)

        self.pencil_density_label = QLabel("68%")
        self.pencil_density_slider = QSlider(Qt.Orientation.Horizontal)
        self.pencil_density_slider.setRange(5, 100)
        self.pencil_density_slider.setValue(68)
        self.pencil_density_slider.valueChanged.connect(self.set_pencil_density)

        self.pencil_pressure_size_checkbox = QCheckBox("压感控制尺寸")
        self.pencil_pressure_size_checkbox.toggled.connect(self.set_pencil_pressure_size_enabled)
        self.pencil_pressure_size_checkbox.setChecked(True)

        self.pencil_pressure_density_checkbox = QCheckBox("压感控制沉积")
        self.pencil_pressure_density_checkbox.toggled.connect(self.set_pencil_pressure_density_enabled)
        self.pencil_pressure_density_checkbox.setChecked(True)

        self.pencil_tip_variation_label = QLabel("20%")
        self.pencil_tip_variation_slider = QSlider(Qt.Orientation.Horizontal)
        self.pencil_tip_variation_slider.setRange(0, 100)
        self.pencil_tip_variation_slider.setValue(20)
        self.pencil_tip_variation_slider.valueChanged.connect(self.set_pencil_tip_variation)

        self.gradient_end_color_combo = QComboBox()
        self.gradient_end_color_combo.addItem("透明", "#00000000")
        self.gradient_end_color_combo.addItem("白", "#ffffff")
        self.gradient_end_color_combo.addItem("黑", "#000000")
        self.gradient_end_color_combo.addItem("红", "#ef4444")
        self.gradient_end_color_combo.addItem("蓝", "#2563eb")
        self.gradient_end_color_combo.currentIndexChanged.connect(self.set_gradient_end_color)

        self.soft_debug_combo = QComboBox()
        self.soft_debug_combo.addItem("关闭", "off")
        self.soft_debug_combo.addItem("原始轨迹", "raw")
        self.soft_debug_combo.addItem("重采样轨迹", "resampled")
        self.soft_debug_combo.addItem("胶囊边界", "capsules")
        self.soft_debug_combo.addItem("覆盖蒙版", "mask")
        self.soft_debug_combo.addItem("最终着色", "final")
        self.soft_debug_combo.currentIndexChanged.connect(self.set_soft_debug_mode)

        self.clear_drawing_button = QPushButton("清空当前帧")
        self.clear_drawing_button.clicked.connect(self.clear_current_drawing_layer)

        self.practice_scale_combo = QComboBox()
        self.practice_scale_combo.addItem("1x", 1)
        self.practice_scale_combo.addItem("2x", 2)
        self.practice_scale_combo.addItem("3x", 3)
        self.practice_scale_combo.addItem("4x", 4)
        self.practice_scale_combo.setCurrentIndex(2)
        self.practice_scale_combo.currentIndexChanged.connect(self.set_practice_scale)

        self.reference_size_combo = QComboBox()
        self.reference_size_combo.addItem("原始尺寸", False)
        self.reference_size_combo.addItem("匹配练习画布尺寸", True)
        self.reference_size_combo.setCurrentIndex(1)
        self.reference_size_combo.currentIndexChanged.connect(self.refresh_current_frame)

        self.grid_checkbox = QCheckBox("网格")
        self.grid_checkbox.toggled.connect(self.set_grid_enabled)

        self.grid_size_combo = QComboBox()
        self.grid_size_combo.addItem("25 px", 25)
        self.grid_size_combo.addItem("50 px", 50)
        self.grid_size_combo.addItem("100 px", 100)
        self.grid_size_combo.setCurrentIndex(1)
        self.grid_size_combo.currentIndexChanged.connect(self.set_grid_size)

        self.grid_color_combo = QComboBox()
        self.grid_color_combo.addItem("浅灰", "#9ca3af")
        self.grid_color_combo.addItem("深灰", "#374151")
        self.grid_color_combo.addItem("蓝色", "#2563eb")
        self.grid_color_combo.addItem("红色", "#dc2626")
        self.grid_color_combo.addItem("绿色", "#16a34a")
        self.grid_color_combo.setCurrentIndex(2)
        self.grid_color_combo.currentIndexChanged.connect(self.set_grid_color)

        self.grid_opacity_combo = QComboBox()
        self.grid_opacity_combo.addItem("20%", 0.2)
        self.grid_opacity_combo.addItem("40%", 0.4)
        self.grid_opacity_combo.addItem("60%", 0.6)
        self.grid_opacity_combo.addItem("80%", 0.8)
        self.grid_opacity_combo.setCurrentIndex(1)
        self.grid_opacity_combo.currentIndexChanged.connect(self.set_grid_opacity)

        self.view_mode_combo = QComboBox()
        self.view_mode_combo.addItems(["叠加模式", "左右对比模式", "只看参考", "只看临摹", "悬浮参考"])
        self.view_mode_combo.setCurrentIndex(1)
        self.view_mode_combo.currentIndexChanged.connect(self.request_view_mode)

        self.settings_button = QPushButton("画布设置")
        self.settings_button.clicked.connect(self.toggle_settings_panel)

        self.frame_label = QLabel()
        self.frame_label.setMinimumWidth(150)

        self.onion_skin_checkbox = QCheckBox("Onion Skin")
        self.onion_skin_checkbox.toggled.connect(self.set_onion_skin_enabled)
        self.onion_loop_checkbox = QCheckBox("首尾帧洋葱皮互通")
        self.onion_loop_checkbox.toggled.connect(self.set_onion_loop_enabled)

        self.previous_onion_color_combo = self._create_onion_color_combo()
        self.previous_onion_color_combo.setCurrentIndex(0)
        self.previous_onion_color_combo.currentIndexChanged.connect(self.set_previous_onion_skin_color)

        self.next_onion_color_combo = self._create_onion_color_combo()
        self.next_onion_color_combo.setCurrentIndex(2)
        self.next_onion_color_combo.currentIndexChanged.connect(self.set_next_onion_skin_color)

        self.onion_opacity_label = QLabel("100%")
        self.onion_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.onion_opacity_slider.setRange(0, 100)
        self.onion_opacity_slider.setValue(100)
        self.onion_opacity_slider.valueChanged.connect(self.set_onion_skin_opacity)

        self.layer_group_list = QListWidget()
        self.layer_group_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.layer_group_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.layer_group_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.layer_group_list.setToolTip("拖拽调整显示顺序；双击名称可重命名")
        self.layer_group_list.currentItemChanged.connect(self._layer_group_selection_changed)
        self.layer_group_list.itemChanged.connect(self._layer_group_item_changed)
        self.layer_group_list.model().rowsMoved.connect(self._layer_group_rows_moved)
        self.layer_group_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.layer_group_list.customContextMenuRequested.connect(self._show_layer_group_context_menu)
        self.paper_visibility_checkbox = QCheckBox("纸张背景")
        self.paper_visibility_checkbox.setChecked(self.paper_visible)
        self.paper_visibility_checkbox.setToolTip("固定在所有图层组下方；隐藏后显示透明棋盘格")
        self.paper_visibility_checkbox.toggled.connect(self.set_paper_visible)
        self.add_layer_group_button = QPushButton("＋ 新建图层组")
        self.add_layer_group_button.clicked.connect(self.add_layer_group)

        self._build_ui()
        app = QApplication.instance()
        if app is not None:
            # Tab is otherwise consumed by Qt focus navigation before this window
            # receives keyPressEvent.
            app.installEventFilter(self)
        self.set_view_mode(self.view_mode_combo.currentIndex())
        self._configure_shortcuts()
        self._load_timeline()

        if self.frame_paths:
            self.timeline.setCurrentRow(0)
        else:
            self.frame_label.setText("No PNG frames found")

        self._update_play_icon()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        for timer in (
            self.play_timer,
            self._frame_switch_timer,
            self._frame_preload_timer,
            self._view_switch_timer,
            self._thumbnail_refresh_timer,
        ):
            timer.stop()
        self.reference_float_window.close()
        super().closeEvent(event)

    @property
    def frame_paths(self) -> list[Path]:
        return [frame.path for frame in self.frames]

    @frame_paths.setter
    def frame_paths(self, paths: list[Path]) -> None:
        self.frames = [FrameData(frame_id=uuid4().hex, path=Path(path)) for path in paths]
        self.playback_range_start = 0
        self.playback_range_end = max(0, len(self.frames) - 1)
        if hasattr(self, "layer_groups"):
            self._reset_layer_groups()

    @property
    def frame_durations(self) -> list[int]:
        return [frame.duration for frame in self.frames]

    @frame_durations.setter
    def frame_durations(self, values: list[int]) -> None:
        for frame, value in zip(self.frames, values):
            frame.duration = max(1, int(value))

    @property
    def frame_exposures(self) -> list[int]:
        return [frame.exposure for frame in self.frames]

    @frame_exposures.setter
    def frame_exposures(self, values: list[int]) -> None:
        for frame, value in zip(self.frames, values):
            frame.exposure = max(1, int(value))

    @property
    def _project_reference_images(self) -> FrameFieldMapping:
        return self._reference_images_by_index

    @_project_reference_images.setter
    def _project_reference_images(self, values: dict[int, QImage]) -> None:
        self._reference_images_by_index.clear()
        for index, image in values.items():
            if 0 <= index < len(self.frames):
                self.frames[index].reference_image = image

    def _build_ui(self) -> None:
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._create_top_toolbar())

        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.overlay_canvas)
        self.view_stack.addWidget(self._create_compare_view())
        self.view_stack.addWidget(self.reference_only_canvas)
        self.view_stack.addWidget(self.trace_only_canvas)
        self.float_reference_view = self._create_float_reference_view()
        self.view_stack.addWidget(self.float_reference_view)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(6, 6, 6, 6)
        self.canvas_timeline_splitter = QSplitter(Qt.Orientation.Vertical)
        self.canvas_timeline_splitter.setChildrenCollapsible(False)
        self.canvas_timeline_splitter.setHandleWidth(7)
        self.canvas_timeline_splitter.addWidget(self._create_canvas_workspace())
        self.canvas_timeline_splitter.addWidget(self._create_timeline_panel())
        self.canvas_timeline_splitter.setStretchFactor(0, 1)
        self.canvas_timeline_splitter.setStretchFactor(1, 0)
        self.canvas_timeline_splitter.setSizes([580, 220])
        central_layout.addWidget(self.canvas_timeline_splitter, 1)
        self.setCentralWidget(central)

        self.settings_dock = QDockWidget("画布设置", self)
        self.settings_dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        self.settings_dock.setWidget(self._create_settings_panel())
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.settings_dock)
        self.settings_dock.hide()

    def _create_canvas_workspace(self) -> QWidget:
        workspace = QWidget()
        layout = QHBoxLayout(workspace)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.view_stack, 1)

        layer_panel = QGroupBox("画布图层组")
        layer_panel.setFixedWidth(210)
        layer_layout = QVBoxLayout(layer_panel)
        layer_layout.setContentsMargins(8, 8, 8, 8)
        layer_layout.addWidget(self.layer_group_list, 1)
        layer_layout.addWidget(self.paper_visibility_checkbox)
        layer_layout.addWidget(self.add_layer_group_button)
        layout.addWidget(layer_panel)
        self._rebuild_layer_group_list()
        return workspace

    def _reset_layer_groups(self) -> None:
        self.layer_groups = [LayerGroup(uuid4().hex, "图层组 1")]
        self.active_layer_group_id = self.layer_groups[0].group_id
        self._layer_stroke_clipboard = None
        self.paper_visible = True
        self.current_index = max(0, min(self.current_index, max(0, len(self.frames) - 1)))
        self._pending_stroke_before.clear()
        self._drawing_operation_context.clear()
        if hasattr(self, "layer_group_list"):
            self._rebuild_layer_group_list()
        if hasattr(self, "paper_visibility_checkbox"):
            self.set_paper_visible(True)
        if hasattr(self, "timeline_rows_layout"):
            self._rebuild_group_timeline_rows()

    def _active_layer_group(self) -> LayerGroup:
        return next(
            (group for group in self.layer_groups if group.group_id == self.active_layer_group_id),
            self.layer_groups[0],
        )

    def set_paper_visible(self, visible: bool) -> None:
        visible = bool(visible)
        changed = visible != self.paper_visible
        self.paper_visible = visible
        for checkbox_name in ("paper_visibility_checkbox", "timeline_paper_visibility_checkbox"):
            checkbox = getattr(self, checkbox_name, None)
            if checkbox is not None and checkbox.isChecked() != visible:
                checkbox.blockSignals(True)
                checkbox.setChecked(visible)
                checkbox.blockSignals(False)
        for canvas in (self.compare_trace_canvas, self.trace_only_canvas, self.float_trace_canvas):
            canvas.set_paper_visible(visible)
        paper_track = getattr(self, "timeline_paper_track", None)
        if paper_track is not None:
            paper_track.update()
        paper_check = getattr(self, "timeline_paper_visibility_checkbox", None)
        if paper_check is not None:
            paper_check.setStyleSheet(
                "QCheckBox { color: #d1d5db; padding-left: 3px; }"
                if visible
                else "QCheckBox { color: #7b8491; padding-left: 3px; }"
            )
        if changed:
            self._invalidate_composite_thumbnails()

    def _rebuild_layer_group_list(self) -> None:
        self._updating_layer_list = True
        try:
            self.layer_group_list.clear()
            active_item = None
            for group in self.layer_groups:
                item = QListWidgetItem(group.name)
                item.setData(Qt.ItemDataRole.UserRole, group.group_id)
                item.setFlags(
                    item.flags()
                    | Qt.ItemFlag.ItemIsEditable
                    | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsDragEnabled
                    | Qt.ItemFlag.ItemIsDropEnabled
                )
                item.setCheckState(Qt.CheckState.Checked if group.visible else Qt.CheckState.Unchecked)
                self.layer_group_list.addItem(item)
                if group.group_id == self.active_layer_group_id:
                    active_item = item
            if active_item is not None:
                self.layer_group_list.setCurrentItem(active_item)
        finally:
            self._updating_layer_list = False

    def add_layer_group(self) -> None:
        number = 1
        names = {group.name for group in self.layer_groups}
        while f"图层组 {number}" in names:
            number += 1
        group = LayerGroup(uuid4().hex, f"图层组 {number}")
        self.layer_groups.insert(0, group)
        self._rebuild_layer_group_list()
        item = self.layer_group_list.item(0)
        self.layer_group_list.setCurrentItem(item)
        self.layer_group_list.editItem(item)
        self._rebuild_group_timeline_rows()

    def duplicate_layer_group(self, group_id: str | None = None) -> LayerGroup | None:
        self._store_active_layer_group()
        source = next(
            (group for group in self.layer_groups if group.group_id == (group_id or self.active_layer_group_id)),
            None,
        )
        if source is None:
            return None
        names = {group.name for group in self.layer_groups}
        base_name = f"{source.name} 副本"
        name = base_name
        suffix = 2
        while name in names:
            name = f"{base_name} {suffix}"
            suffix += 1
        duplicate = LayerGroup(
            uuid4().hex,
            name,
            source.visible,
            {
                frame_id: image.copy()
                for frame_id, image in source.drawings.items()
                if image is not None and not image.isNull()
            },
        )
        source_index = self.layer_groups.index(source)
        self.layer_groups.insert(source_index, duplicate)
        self._rebuild_layer_group_list()
        duplicate_item = next(
            (
                self.layer_group_list.item(index)
                for index in range(self.layer_group_list.count())
                if str(self.layer_group_list.item(index).data(Qt.ItemDataRole.UserRole)) == duplicate.group_id
            ),
            None,
        )
        if duplicate_item is not None:
            self.layer_group_list.setCurrentItem(duplicate_item)
        self._rebuild_group_timeline_rows()
        self._invalidate_composite_thumbnails()
        return duplicate

    def _show_layer_group_context_menu(self, position: QPoint) -> None:
        item = self.layer_group_list.itemAt(position)
        if item is None:
            return
        group_id = str(item.data(Qt.ItemDataRole.UserRole))
        menu = QMenu(self.layer_group_list)
        menu.addAction("复制图层组", lambda: self.duplicate_layer_group(group_id))
        menu.exec(self.layer_group_list.viewport().mapToGlobal(position))

    def _store_active_layer_group(self) -> None:
        group = self._active_layer_group()
        group.drawings = {
            frame.frame_id: frame.drawing
            for frame in self.frames
            if frame.drawing is not None and not frame.drawing.isNull()
        }
        group.histories = {
            frame.frame_id: frame.history
            for frame in self.frames
            if frame.history is not None
        }

    def _activate_layer_group(self, group_id: str, *, focus_timeline: bool = True) -> None:
        if group_id == self.active_layer_group_id:
            self.sync_drawing_layer_to_views()
            self._update_timeline_group_highlight()
            if focus_timeline:
                self._focus_active_group_timeline()
            return
        target = next((group for group in self.layer_groups if group.group_id == group_id), None)
        if target is None:
            return
        self._store_active_layer_group()
        self.active_layer_group_id = target.group_id
        for frame in self.frames:
            frame.drawing = target.drawings.get(frame.frame_id)
            frame.history = target.histories.get(frame.frame_id)
        self._pending_stroke_before.clear()
        self._history_memory.reindex(self._histories_by_id())
        self.sync_drawing_layer_to_views()
        self._update_timeline_group_highlight()
        if focus_timeline:
            self._focus_active_group_timeline()

    def _layer_group_selection_changed(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if self._updating_layer_list or current is None:
            return
        self._activate_layer_group(str(current.data(Qt.ItemDataRole.UserRole)))

    def _layer_group_item_changed(self, item: QListWidgetItem) -> None:
        if self._updating_layer_list:
            return
        group_id = str(item.data(Qt.ItemDataRole.UserRole))
        group = next((candidate for candidate in self.layer_groups if candidate.group_id == group_id), None)
        if group is None:
            return
        cleaned_name = item.text().strip()
        if not cleaned_name:
            self._updating_layer_list = True
            item.setText(group.name)
            self._updating_layer_list = False
        else:
            group.name = cleaned_name
        visible = item.checkState() == Qt.CheckState.Checked
        visibility_changed = visible != group.visible
        group.visible = visible
        timeline_check = getattr(self, "group_timeline_visibility_checks", {}).get(group_id)
        if timeline_check is not None:
            timeline_check.blockSignals(True)
            timeline_check.setText(group.name)
            timeline_check.setChecked(group.visible)
            timeline_check.blockSignals(False)
        if visibility_changed:
            self.sync_drawing_layer_to_views()
            self._invalidate_composite_thumbnails()

    def _layer_group_rows_moved(self, *_args) -> None:
        if self._updating_layer_list:
            return
        groups_by_id = {group.group_id: group for group in self.layer_groups}
        ordered_ids = [
            str(self.layer_group_list.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.layer_group_list.count())
        ]
        if len(ordered_ids) == len(groups_by_id) and set(ordered_ids) == set(groups_by_id):
            self.layer_groups = [groups_by_id[group_id] for group_id in ordered_ids]
            self.sync_drawing_layer_to_views()
            self._invalidate_composite_thumbnails()
            self._rebuild_group_timeline_rows()

    def _configure_group_timeline(self, timeline: TimelineListWidget) -> None:
        timeline.setViewMode(QListView.ViewMode.IconMode)
        timeline.setFlow(QListView.Flow.LeftToRight)
        timeline.setWrapping(False)
        timeline.setMovement(QListView.Movement.Static)
        timeline.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        timeline.setDefaultDropAction(Qt.DropAction.MoveAction)
        timeline.setDragEnabled(True)
        timeline.setAcceptDrops(True)
        timeline.viewport().setAcceptDrops(True)
        timeline.setDropIndicatorShown(False)
        timeline.setDragDropOverwriteMode(False)
        timeline.setResizeMode(QListView.ResizeMode.Adjust)
        timeline.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        timeline.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        timeline.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        timeline.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        timeline.setFrameShape(QListWidget.Shape.NoFrame)
        timeline.setSpacing(0)
        timeline.setUniformItemSizes(True)
        timeline.setIconSize(self.timeline_geometry.thumbnail_size())
        timeline.setGridSize(QSize(self.timeline_geometry.frame_width(), self.timeline_geometry.row_height()))
        timeline.setFixedHeight(self.timeline_geometry.row_height())
        timeline.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        timeline.setStyleSheet(
            "QListWidget { background: #242830; border: none; outline: none; }"
            "QListWidget::item { background: transparent; border: none; padding: 0; margin: 0; }"
            "QListWidget::item:selected { background: transparent; border: none; }"
        )
        timeline.set_timeline_geometry(self.timeline_geometry, lambda: self.current_index)
        timeline.setItemDelegate(TimelineFrameDelegate(timeline))
        timeline.set_vertical_wheel_handler(self._scroll_timeline_tracks_vertically)
        self._install_timeline_wheel_filter(timeline)
        self._install_timeline_wheel_filter(timeline.viewport())
        self._install_timeline_wheel_filter(timeline.horizontalScrollBar())
        self._install_timeline_wheel_filter(timeline.verticalScrollBar())

    def _rebuild_group_timeline_rows(self) -> None:
        if not hasattr(self, "timeline_rows_layout"):
            return
        self.timeline.setParent(None)
        while self.timeline_rows_layout.count():
            layout_item = self.timeline_rows_layout.takeAt(0)
            widget = layout_item.widget()
            if widget is not None:
                widget.deleteLater()

        self.group_timeline_widgets = {}
        self.group_timeline_row_hosts = {}
        self.group_timeline_visibility_checks: dict[str, QCheckBox] = {}
        self.current_index = max(0, min(self.current_index, max(0, len(self.frames) - 1)))
        selected_frame_id = self.frames[self.current_index].frame_id if self.frames else ""
        for group_index, group in enumerate(self.layer_groups):
            row = QWidget()
            row.setObjectName("timelineGroupRow")
            row.setFixedHeight(self.timeline_geometry.row_height())
            row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.group_timeline_row_hosts[group.group_id] = row
            self._install_timeline_wheel_filter(row)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(1)

            visible_check = QCheckBox(group.name)
            visible_check.setChecked(group.visible)
            visible_check.setFixedWidth(TIMELINE_LABEL_WIDTH)
            visible_check.setFixedHeight(self.timeline_geometry.row_height())
            visible_check.setToolTip("勾选控制显示；点击名称切换到该动画组")
            self._install_timeline_wheel_filter(visible_check)
            visible_check.clicked.connect(
                lambda checked, group_id=group.group_id: self._timeline_group_visibility_changed(group_id, checked)
            )
            row_layout.addWidget(visible_check)
            self.group_timeline_visibility_checks[group.group_id] = visible_check

            timeline = self.timeline if group_index == 0 else TimelineListWidget()
            if timeline is not self.timeline:
                self._configure_group_timeline(timeline)
                timeline.currentRowChanged.connect(
                    lambda index, group_id=group.group_id, source=timeline: self._group_timeline_row_changed(
                        group_id, source, index
                    )
                )
                timeline.drag_started.connect(self._set_timeline_drag_active)
                timeline.drag_finished.connect(self._clear_timeline_drag_active)
                timeline.frame_move_requested.connect(self._timeline_frame_move_requested)
                timeline.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                timeline.customContextMenuRequested.connect(
                    lambda position, group_id=group.group_id, source=timeline: self._show_group_timeline_context_menu(
                        group_id, source, position
                    )
                )
            else:
                self._primary_timeline_group_id = group.group_id
            self.group_timeline_widgets[group.group_id] = timeline
            timeline.blockSignals(True)
            timeline.clear()
            for frame_index, frame in enumerate(self.frames):
                item = self._new_timeline_item_for_widget(timeline, frame, frame_index)
                timeline.addItem(item)
                if frame.frame_id == selected_frame_id:
                    timeline.setCurrentItem(item)
            timeline.blockSignals(False)
            row_layout.addWidget(timeline, 1)
            self.timeline_rows_layout.addWidget(row, 0)
        paper_row = QWidget()
        paper_row.setObjectName("timelinePaperRow")
        paper_row.setFixedHeight(self.timeline_geometry.row_height())
        paper_layout = QHBoxLayout(paper_row)
        paper_layout.setContentsMargins(0, 0, 0, 0)
        paper_layout.setSpacing(1)
        self.timeline_paper_visibility_checkbox = QCheckBox("纸张背景")
        self.timeline_paper_visibility_checkbox.setFixedWidth(TIMELINE_LABEL_WIDTH)
        self.timeline_paper_visibility_checkbox.setFixedHeight(self.timeline_geometry.row_height())
        self.timeline_paper_visibility_checkbox.setChecked(self.paper_visible)
        self.timeline_paper_visibility_checkbox.toggled.connect(self.set_paper_visible)
        self._install_timeline_wheel_filter(paper_row)
        self._install_timeline_wheel_filter(self.timeline_paper_visibility_checkbox)
        paper_layout.addWidget(self.timeline_paper_visibility_checkbox)
        self.timeline_paper_track = TimelinePaperTrack(self)
        self._install_timeline_wheel_filter(self.timeline_paper_track)
        paper_layout.addWidget(self.timeline_paper_track, 1)
        self.timeline_rows_layout.addWidget(paper_row, 0)
        self.timeline_paper_row_host = paper_row
        self.set_paper_visible(self.paper_visible)
        self._update_timeline_group_highlight()
        self._focus_active_group_timeline()
        self._update_timeline_scroll_range()
        self._apply_timeline_scroll_x(self.timeline_geometry.scroll_x)
        self._schedule_visible_thumbnail_refresh()

    def _new_timeline_item_for_widget(
        self, timeline: TimelineListWidget, frame: FrameData, index: int
    ) -> QListWidgetItem:
        suffix = f" x{frame.exposure}" if frame.exposure > 1 else ""
        item = QListWidgetItem(f"{index + 1}{suffix}")
        item.setData(Qt.ItemDataRole.UserRole, frame.frame_id)
        item.setSizeHint(QSize(self.timeline_geometry.frame_width(), self.timeline_geometry.row_height()))
        item.setIcon(QIcon(self._thumbnail_placeholder()))
        return item

    def _timeline_group_visibility_changed(self, group_id: str, visible: bool) -> None:
        group = next((candidate for candidate in self.layer_groups if candidate.group_id == group_id), None)
        if group is None:
            return
        group.visible = visible
        self._activate_layer_group(group_id)
        self._rebuild_layer_group_list()
        self._invalidate_composite_thumbnails()

    def _group_timeline_row_changed(
        self, group_id: str, timeline: TimelineListWidget, index: int
    ) -> None:
        if self._timeline_drag_active or index < 0:
            return
        self._request_timeline_frame(group_id, index)

    def _update_timeline_group_highlight(self) -> None:
        for group_id, check in getattr(self, "group_timeline_visibility_checks", {}).items():
            if group_id == self.active_layer_group_id:
                check.setStyleSheet(
                    "QCheckBox { font-weight: 600; color: #8abaff; background: #263650; padding-left: 3px; }"
                )
            else:
                check.setStyleSheet("QCheckBox { color: #c8ced8; background: transparent; padding-left: 3px; }")
        for group_id, row in getattr(self, "group_timeline_row_hosts", {}).items():
            row.setStyleSheet(
                "QWidget#timelineGroupRow { background: #242830; border: none; border-bottom: 1px solid #303640; }"
            )
        if hasattr(self, "timeline_ruler"):
            self.timeline_ruler.set_timeline(self.group_timeline_widgets.get(self.active_layer_group_id))
        for group_id, timeline in getattr(self, "group_timeline_widgets", {}).items():
            timeline.setProperty("activeTimelineTrack", group_id == self.active_layer_group_id)
            timeline.viewport().update()
        paper_track = getattr(self, "timeline_paper_track", None)
        if paper_track is not None:
            paper_track.update()

    def _focus_active_group_timeline(self) -> None:
        if not hasattr(self, "timeline_rows_scroll"):
            return
        row = self.group_timeline_row_hosts.get(self.active_layer_group_id)
        timeline = self.group_timeline_widgets.get(self.active_layer_group_id)
        if row is not None:
            self.timeline_rows_scroll.ensureWidgetVisible(row, 0, 10)
        if timeline is not None and self.frames:
            frame_id = self.frames[self.current_index].frame_id
            item = next(
                (
                    timeline.item(index)
                    for index in range(timeline.count())
                    if str(timeline.item(index).data(Qt.ItemDataRole.UserRole)) == frame_id
                ),
                None,
            )
            if item is not None:
                timeline.blockSignals(True)
                try:
                    timeline.setCurrentItem(item)
                finally:
                    timeline.blockSignals(False)
                self._ensure_timeline_frame_visible(self.current_index)

    def _update_timeline_scroll_range(self) -> None:
        if not hasattr(self, "timeline_horizontal_scrollbar"):
            return
        if self.timeline_horizontal_scrollbar.isSliderDown():
            return
        viewport_width = max(0, self.timeline.viewport().width())
        maximum = max(0, self.timeline_geometry.content_width(len(self.frames)) - viewport_width)
        scrollbar = self.timeline_horizontal_scrollbar
        scrollbar.setPageStep(max(1, viewport_width))
        scrollbar.setSingleStep(max(1, self.timeline_geometry.frame_width() // 4))
        scrollbar.setRange(0, maximum)
        if self.timeline_geometry.scroll_x > maximum:
            scrollbar.setValue(maximum)

    def _apply_timeline_scroll_x(self, value: int) -> None:
        value = max(0, int(value))
        self.timeline_scroll_x = value
        self.timeline_geometry.scroll_x = value
        timelines = list(getattr(self, "group_timeline_widgets", {}).values()) or [self.timeline]
        self._syncing_timeline_scroll = True
        try:
            for timeline in timelines:
                timeline.horizontalScrollBar().setValue(value)
                timeline.viewport().update()
        finally:
            self._syncing_timeline_scroll = False
        if hasattr(self, "timeline_ruler"):
            self.timeline_ruler.update()
        paper_track = getattr(self, "timeline_paper_track", None)
        if paper_track is not None:
            paper_track.update()
        if hasattr(self, "timeline_horizontal_scrollbar") and not self.timeline_horizontal_scrollbar.isSliderDown():
            self._schedule_visible_thumbnail_refresh(delay_ms=16)

    def _finish_timeline_horizontal_scroll(self) -> None:
        self._schedule_visible_thumbnail_refresh(delay_ms=16)

    def _install_timeline_wheel_filter(self, widget: QWidget) -> None:
        widget.setProperty("timelineWheelTarget", True)
        widget.installEventFilter(self)

    def _scroll_timeline_tracks_vertically(self, event) -> bool:
        if not hasattr(self, "timeline_rows_scroll"):
            return True
        pixel_delta = event.pixelDelta()
        delta = pixel_delta.y()
        if not delta and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            delta = pixel_delta.x()
        if not delta:
            angle_delta = event.angleDelta()
            delta = angle_delta.y()
            if not delta and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                delta = angle_delta.x()
            if delta:
                delta = round(delta / 120.0 * 32)
        scrollbar = self.timeline_rows_scroll.verticalScrollBar()
        if delta and scrollbar.maximum() > scrollbar.minimum():
            old_value = scrollbar.value()
            scrollbar.setValue(old_value - int(delta))
            if scrollbar.value() != old_value:
                self._schedule_visible_thumbnail_refresh(delay_ms=16)
        event.accept()
        return True

    def _ensure_timeline_frame_visible(self, index: int) -> None:
        if not hasattr(self, "timeline_horizontal_scrollbar") or not self.frames:
            return
        index = max(0, min(index, len(self.frames) - 1))
        viewport_width = max(1, self.timeline.viewport().width())
        frame_width = self.timeline_geometry.frame_width()
        content_left = index * frame_width
        content_right = content_left + frame_width
        scroll_x = self.timeline_geometry.scroll_x
        if content_left < scroll_x:
            self.timeline_horizontal_scrollbar.setValue(content_left)
        elif content_right > scroll_x + viewport_width:
            self.timeline_horizontal_scrollbar.setValue(content_right - viewport_width)

    def _request_timeline_frame(self, group_id: str, index: int) -> None:
        if self._timeline_drag_active or index < 0 or index >= len(self.frames):
            return
        if group_id != self.active_layer_group_id:
            self._activate_layer_group(group_id, focus_timeline=False)
        frame_id = self.frames[index].frame_id
        # Move the global playhead immediately; canvas loading is coalesced by
        # frame_id so rapid clicks still render only the final requested frame.
        self.current_index = index
        self._select_timeline_frame_id(frame_id)
        if hasattr(self, "timeline_ruler"):
            self.timeline_ruler.update()
        for timeline in getattr(self, "group_timeline_widgets", {}).values():
            timeline.viewport().update()
        paper_track = getattr(self, "timeline_paper_track", None)
        if paper_track is not None:
            paper_track.update()
        self._pending_timeline_frame_id = frame_id
        self._frame_switch_timer.start(16)

    def _group_frame_thumbnail(self, group: LayerGroup, frame_id: str) -> QPixmap:
        icon_size = self.timeline_geometry.thumbnail_size()
        cache_key = self._group_thumbnail_cache_key(group, frame_id)
        cached = self._group_thumbnail_cache.get(cache_key)
        if cached is not None:
            return cached
        thumbnail = QImage(icon_size, QImage.Format.Format_ARGB32_Premultiplied)
        thumbnail.fill(Qt.GlobalColor.transparent)
        reference = self._reference_image_for_thumbnail(frame_id)
        target_rect = QRect(QPoint(), icon_size)
        drawing = self._drawing_for_group(group, frame_id)
        aspect_source = reference if not reference.isNull() else drawing
        if aspect_source is not None and not aspect_source.isNull():
            target_size = aspect_source.size().scaled(icon_size, Qt.AspectRatioMode.KeepAspectRatio)
            target_rect = QRect(
                (icon_size.width() - target_size.width()) // 2,
                (icon_size.height() - target_size.height()) // 2,
                target_size.width(),
                target_size.height(),
            )
        painter = QPainter(thumbnail)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        has_preview = (
            self.thumbnail_display_mode == "reference" and not reference.isNull()
        ) or (
            self.thumbnail_display_mode == "drawing" and drawing is not None and not drawing.isNull()
        ) or (
            self.thumbnail_display_mode == "composite"
            and (not reference.isNull() or (drawing is not None and not drawing.isNull()))
        )
        if has_preview:
            # The light background belongs only to this temporary preview.
            painter.fillRect(target_rect, QColor("#f0f1f2"))
        if self.thumbnail_display_mode in ("reference", "composite") and not reference.isNull():
            painter.drawImage(target_rect, reference)
        if self.thumbnail_display_mode != "reference":
            if drawing is not None and not drawing.isNull():
                painter.drawImage(target_rect, drawing)
        painter.end()
        result = QPixmap.fromImage(thumbnail)
        self._group_thumbnail_cache[cache_key] = result
        return result

    def _group_thumbnail_cache_key(self, group: LayerGroup, frame_id: str) -> tuple[object, ...]:
        size = self.timeline_geometry.thumbnail_size()
        return (
            frame_id,
            group.group_id,
            self.thumbnail_display_mode,
            size.width(),
            size.height(),
            self._thumbnail_content_versions.get((frame_id, group.group_id), 0),
        )

    def _refresh_group_timeline_icons(self, frame_id: str | None = None) -> None:
        if not self.frames:
            return
        viewport_width = self.timeline.viewport().width()
        first, last = self.timeline_geometry.visible_frame_range(viewport_width, len(self.frames))
        for group in self._visible_timeline_groups():
            timeline = self.group_timeline_widgets.get(group.group_id)
            if timeline is None:
                continue
            for index in range(max(0, first), min(timeline.count() - 1, last) + 1):
                item = timeline.item(index)
                item_frame_id = str(item.data(Qt.ItemDataRole.UserRole))
                if frame_id is None or item_frame_id == frame_id:
                    item.setIcon(QIcon(self._group_frame_thumbnail(group, item_frame_id)))

    def _visible_timeline_groups(self) -> list[LayerGroup]:
        if not hasattr(self, "timeline_rows_scroll") or not self.timeline_rows_scroll.isVisible():
            return list(self.layer_groups)
        scrollbar = self.timeline_rows_scroll.verticalScrollBar()
        visible_rect = QRect(
            0,
            scrollbar.value(),
            self.timeline_rows_scroll.viewport().width(),
            self.timeline_rows_scroll.viewport().height(),
        )
        return [
            group
            for group in self.layer_groups
            if (row := self.group_timeline_row_hosts.get(group.group_id)) is not None
            and row.geometry().intersects(visible_rect)
        ]

    def _create_timeline_panel(self) -> QWidget:
        panel = QWidget()
        self._install_timeline_wheel_filter(panel)
        panel.setStyleSheet("QWidget { background: #20242b; color: #d1d5db; }")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("缩略图显示"))
        controls.addWidget(self.thumbnail_display_combo)
        controls.addSpacing(12)
        controls.addWidget(QLabel("时间轴倍率"))
        controls.addWidget(self.timeline_zoom_slider)
        controls.addWidget(self.timeline_zoom_value_label)
        self._install_timeline_wheel_filter(self.thumbnail_display_combo)
        self._install_timeline_wheel_filter(self.timeline_zoom_slider)
        self._install_timeline_wheel_filter(self.timeline_zoom_value_label)
        controls.addStretch(1)
        layout.addLayout(controls)

        ruler_row = QHBoxLayout()
        ruler_row.setContentsMargins(0, 0, 0, 0)
        ruler_row.setSpacing(1)
        ruler_spacer = QWidget()
        self._install_timeline_wheel_filter(ruler_spacer)
        ruler_spacer.setFixedWidth(TIMELINE_LABEL_WIDTH)
        ruler_row.addWidget(ruler_spacer)
        self.timeline_ruler = TimelineRuler(self)
        self._install_timeline_wheel_filter(self.timeline_ruler)
        ruler_row.addWidget(self.timeline_ruler, 1)
        layout.addLayout(ruler_row)

        self.timeline_rows_widget = QWidget()
        self._install_timeline_wheel_filter(self.timeline_rows_widget)
        self.timeline_rows_layout = QVBoxLayout(self.timeline_rows_widget)
        self.timeline_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.timeline_rows_layout.setSpacing(1)
        self.timeline_rows_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.timeline_rows_scroll = QScrollArea()
        self.timeline_rows_scroll.setWidgetResizable(True)
        self.timeline_rows_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.timeline_rows_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.timeline_rows_scroll.setMinimumHeight(96)
        self.timeline_rows_scroll.setWidget(self.timeline_rows_widget)
        self._install_timeline_wheel_filter(self.timeline_rows_scroll.viewport())
        layout.addWidget(self.timeline_rows_scroll)

        scrollbar_row = QHBoxLayout()
        scrollbar_row.setContentsMargins(0, 0, 0, 0)
        scrollbar_row.setSpacing(1)
        scrollbar_spacer = QWidget()
        self._install_timeline_wheel_filter(scrollbar_spacer)
        scrollbar_spacer.setFixedWidth(TIMELINE_LABEL_WIDTH)
        scrollbar_row.addWidget(scrollbar_spacer)
        self.timeline_horizontal_scrollbar = QScrollBar(Qt.Orientation.Horizontal)
        self.timeline_horizontal_scrollbar.setTracking(True)
        self.timeline_horizontal_scrollbar.setSingleStep(max(1, self.timeline_geometry.frame_width() // 4))
        self.timeline_horizontal_scrollbar.valueChanged.connect(self._apply_timeline_scroll_x)
        self.timeline_horizontal_scrollbar.sliderReleased.connect(self._finish_timeline_horizontal_scroll)
        self._install_timeline_wheel_filter(self.timeline_horizontal_scrollbar)
        scrollbar_row.addWidget(self.timeline_horizontal_scrollbar, 1)
        layout.addLayout(scrollbar_row)
        self._rebuild_group_timeline_rows()
        self.set_timeline_zoom()
        return panel

    def _create_top_toolbar(self) -> QToolBar:
        toolbar = QToolBar("工具栏")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.addWidget(self.import_gif_button)
        toolbar.addWidget(self.project_button)
        toolbar.addWidget(self.export_gif_button)
        toolbar.addWidget(QLabel(" 导出 "))
        toolbar.addWidget(self.export_fps_slider)
        toolbar.addWidget(self.export_fps_label)
        toolbar.addWidget(QLabel(" Step "))
        toolbar.addWidget(self.import_step_slider)
        toolbar.addWidget(self.import_step_label)
        toolbar.addSeparator()
        toolbar.addWidget(self.previous_button)
        toolbar.addWidget(self.play_button)
        toolbar.addWidget(self.playback_fps_slider)
        toolbar.addWidget(self.playback_fps_label)
        toolbar.addWidget(self.next_button)
        toolbar.addWidget(self.fit_button)
        toolbar.addSeparator()
        toolbar.addWidget(QLabel(" 视图 "))
        toolbar.addWidget(self.view_mode_combo)
        toolbar.addWidget(self.settings_button)
        toolbar.addSeparator()
        return toolbar

    def _create_compare_view(self) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._create_labeled_canvas("参考帧", self.compare_reference_canvas))
        splitter.addWidget(self._create_labeled_canvas("临摹画布", self.compare_trace_canvas))
        splitter.setSizes([640, 640])
        return splitter

    def _create_float_reference_view(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.float_trace_canvas)
        self.reference_float_window.set_host(host)
        self.reference_float_window.move(28, 28)
        self.reference_float_window.show()
        self.reference_float_window.raise_()
        return host

    def _create_labeled_canvas(self, title: str, canvas: QWidget) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(QLabel(title))
        layout.addWidget(canvas, 1)
        return panel

    def _create_settings_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)

        drawing_group = QGroupBox("绘制")
        drawing_layout = QFormLayout(drawing_group)
        drawing_layout.addRow("工具", self.tool_combo)
        drawing_layout.addRow("颜色", self.brush_color_combo)
        drawing_layout.addRow("粗细", self.brush_size_combo)
        brush_opacity_row = QHBoxLayout()
        brush_opacity_row.addWidget(self.brush_opacity_slider, 1)
        brush_opacity_row.addWidget(self.brush_opacity_label)
        drawing_layout.addRow("透明度", brush_opacity_row)
        drawing_layout.addRow(self.clear_drawing_button)
        layout.addWidget(drawing_group)

        self.soft_brush_settings_group = QGroupBox("软笔参数")
        soft_layout = QFormLayout(self.soft_brush_settings_group)
        brush_hardness_row = QHBoxLayout()
        brush_hardness_row.addWidget(self.brush_hardness_slider, 1)
        brush_hardness_row.addWidget(self.brush_hardness_label)
        soft_layout.addRow("软边硬度", brush_hardness_row)
        soft_layout.addRow("软笔模式", self.soft_renderer_mode_combo)
        gamma_row = QHBoxLayout()
        gamma_row.addWidget(self.soft_edge_gamma_slider, 1)
        gamma_row.addWidget(self.soft_edge_gamma_label)
        soft_layout.addRow("边缘 Gamma", gamma_row)
        soft_layout.addRow(self.soft_pressure_size_checkbox)
        soft_layout.addRow(self.soft_pressure_opacity_checkbox)
        soft_layout.addRow("软笔调试", self.soft_debug_combo)
        layout.addWidget(self.soft_brush_settings_group)

        self.airbrush_settings_group = QGroupBox("喷枪参数")
        airbrush_layout = QFormLayout(self.airbrush_settings_group)
        soft_flow_row = QHBoxLayout()
        soft_flow_row.addWidget(self.soft_flow_slider, 1)
        soft_flow_row.addWidget(self.soft_flow_label)
        airbrush_layout.addRow("喷枪流量", soft_flow_row)
        layout.addWidget(self.airbrush_settings_group)

        self.square_brush_settings_group = QGroupBox("方头笔参数")
        square_layout = QFormLayout(self.square_brush_settings_group)
        square_layout.addRow("方头方向", self.square_brush_angle_combo)
        square_layout.addRow(self.square_pressure_size_checkbox)
        square_strength_row = QHBoxLayout()
        square_strength_row.addWidget(self.square_pressure_strength_slider, 1)
        square_strength_row.addWidget(self.square_pressure_strength_label)
        square_layout.addRow("压感强度", square_strength_row)
        square_min_size_row = QHBoxLayout()
        square_min_size_row.addWidget(self.square_min_pressure_size_slider, 1)
        square_min_size_row.addWidget(self.square_min_pressure_size_label)
        square_layout.addRow("最小尺寸比例", square_min_size_row)
        layout.addWidget(self.square_brush_settings_group)

        self.pencil_settings_group = QGroupBox("铅笔参数")
        pencil_layout = QFormLayout(self.pencil_settings_group)
        pencil_grain_row = QHBoxLayout()
        pencil_grain_row.addWidget(self.pencil_grain_slider, 1)
        pencil_grain_row.addWidget(self.pencil_grain_label)
        pencil_layout.addRow("笔芯颗粒", pencil_grain_row)
        pencil_density_row = QHBoxLayout()
        pencil_density_row.addWidget(self.pencil_density_slider, 1)
        pencil_density_row.addWidget(self.pencil_density_label)
        pencil_layout.addRow("石墨密度", pencil_density_row)
        pencil_layout.addRow(self.pencil_pressure_size_checkbox)
        pencil_layout.addRow(self.pencil_pressure_density_checkbox)
        pencil_variation_row = QHBoxLayout()
        pencil_variation_row.addWidget(self.pencil_tip_variation_slider, 1)
        pencil_variation_row.addWidget(self.pencil_tip_variation_label)
        pencil_layout.addRow("笔尖变化", pencil_variation_row)
        layout.addWidget(self.pencil_settings_group)

        self.gradient_settings_group = QGroupBox("渐变参数")
        gradient_layout = QFormLayout(self.gradient_settings_group)
        gradient_layout.addRow("终点颜色", self.gradient_end_color_combo)
        layout.addWidget(self.gradient_settings_group)

        canvas_group = QGroupBox("画布与网格")
        canvas_layout = QFormLayout(canvas_group)
        canvas_layout.addRow("练习倍率", self.practice_scale_combo)
        canvas_layout.addRow("参考显示", self.reference_size_combo)
        canvas_layout.addRow(self.grid_checkbox)
        canvas_layout.addRow("网格大小", self.grid_size_combo)
        canvas_layout.addRow("网格颜色", self.grid_color_combo)
        canvas_layout.addRow("网格透明度", self.grid_opacity_combo)
        layout.addWidget(canvas_group)

        onion_group = QGroupBox("洋葱皮")
        onion_layout = QVBoxLayout(onion_group)
        onion_layout.addWidget(self.onion_skin_checkbox)
        onion_layout.addWidget(self.onion_loop_checkbox)

        previous_color_row = QHBoxLayout()
        previous_color_row.addWidget(QLabel("前一帧颜色"))
        previous_color_row.addWidget(self.previous_onion_color_combo)
        onion_layout.addLayout(previous_color_row)

        next_color_row = QHBoxLayout()
        next_color_row.addWidget(QLabel("后一帧颜色"))
        next_color_row.addWidget(self.next_onion_color_combo)
        onion_layout.addLayout(next_color_row)

        opacity_row = QHBoxLayout()
        opacity_row.addWidget(QLabel("整体透明度"))
        opacity_row.addWidget(self.onion_opacity_slider, 1)
        opacity_row.addWidget(self.onion_opacity_label)
        onion_layout.addLayout(opacity_row)
        layout.addWidget(onion_group)
        self._update_brush_parameter_visibility()
        layout.addStretch(1)
        return panel

    def _create_onion_color_combo(self) -> QComboBox:
        combo = QComboBox()
        combo.addItem("红", "#ef4444")
        combo.addItem("绿", "#22c55e")
        combo.addItem("蓝", "#3b82f6")
        combo.addItem("黄", "#facc15")
        combo.addItem("紫", "#a855f7")
        return combo

    def _load_timeline(self, selected_frame_id: str | None = None) -> None:
        self._rebuilding_timeline = True
        self._thumbnail_generation += 1
        try:
            self.timeline.clear()
            for index, frame in enumerate(self.frames):
                self.timeline.addItem(self._new_timeline_item(frame, index))
        finally:
            self._rebuilding_timeline = False
        if selected_frame_id:
            self._select_timeline_frame_id(selected_frame_id)
        if hasattr(self, "timeline_rows_layout"):
            self._rebuild_group_timeline_rows()
        self._schedule_visible_thumbnail_refresh()

    def _sync_timeline_from_frames(self, selected_frame_id: str) -> bool:
        """Reorder existing items without clearing icons or exposing an empty list."""
        expected_ids = [frame.frame_id for frame in self.frames]
        current_ids = [
            str(self.timeline.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.timeline.count())
        ]
        if (
            len(current_ids) != len(expected_ids)
            or len(set(current_ids)) != len(current_ids)
            or set(current_ids) != set(expected_ids)
        ):
            return False

        self._rebuilding_timeline = True
        self._thumbnail_generation += 1
        self.timeline.blockSignals(True)
        self.timeline.setUpdatesEnabled(False)
        try:
            for target_index, frame in enumerate(self.frames):
                item = self._timeline_item_for_frame_id(frame.frame_id)
                if item is None:
                    return False
                current_index = self.timeline.row(item)
                if current_index != target_index:
                    retained_item = self.timeline.takeItem(current_index)
                    self.timeline.insertItem(target_index, retained_item)
                suffix = f" x{frame.exposure}" if frame.exposure > 1 else ""
                item.setText(f"{target_index + 1}{suffix}")
                item.setSizeHint(self.timeline.gridSize())
                if item.icon().isNull():
                    item.setIcon(QIcon(self._thumbnail_placeholder()))
            selected_item = self._timeline_item_for_frame_id(selected_frame_id)
            if selected_item is not None:
                self.timeline.setCurrentItem(selected_item)
        finally:
            self.timeline.setUpdatesEnabled(True)
            self.timeline.blockSignals(False)
            self._rebuilding_timeline = False
            self.timeline.viewport().update()
        self._schedule_visible_thumbnail_refresh()
        self._sync_secondary_group_timelines(selected_frame_id)
        self._update_timeline_scroll_range()
        self._apply_timeline_scroll_x(self.timeline_geometry.scroll_x)
        return True

    def _sync_secondary_group_timelines(self, selected_frame_id: str) -> None:
        for timeline in self.group_timeline_widgets.values():
            if timeline is self.timeline:
                continue
            timeline.blockSignals(True)
            timeline.setUpdatesEnabled(False)
            try:
                items_by_id = {
                    str(timeline.item(index).data(Qt.ItemDataRole.UserRole)): timeline.item(index)
                    for index in range(timeline.count())
                }
                if set(items_by_id) != {frame.frame_id for frame in self.frames}:
                    continue
                for target_index, frame in enumerate(self.frames):
                    item = items_by_id[frame.frame_id]
                    current_index = timeline.row(item)
                    if current_index != target_index:
                        timeline.insertItem(target_index, timeline.takeItem(current_index))
                    suffix = f" x{frame.exposure}" if frame.exposure > 1 else ""
                    item.setText(f"{target_index + 1}{suffix}")
                selected = items_by_id.get(selected_frame_id)
                if selected is not None:
                    timeline.setCurrentItem(selected)
            finally:
                timeline.setUpdatesEnabled(True)
                timeline.blockSignals(False)
                timeline.viewport().update()

    def _new_timeline_item(self, frame: FrameData, index: int) -> QListWidgetItem:
        suffix = f" x{frame.exposure}" if frame.exposure > 1 else ""
        item = QListWidgetItem(f"{index + 1}{suffix}")
        item.setData(Qt.ItemDataRole.UserRole, frame.frame_id)
        item.setSizeHint(self.timeline.gridSize())
        item.setIcon(QIcon(self._thumbnail_placeholder()))
        return item

    def _thumbnail_placeholder(self) -> QPixmap:
        icon_size = self.timeline_geometry.thumbnail_size()
        pixmap = QPixmap(icon_size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.fillRect(pixmap.rect(), QColor(32, 36, 43, 115))
        painter.setPen(QPen(QColor("#454c57"), 1))
        painter.drawRect(pixmap.rect().adjusted(0, 0, -1, -1))
        painter.end()
        return pixmap

    def _frame_index_for_id(self, frame_id: str) -> int | None:
        return next((index for index, frame in enumerate(self.frames) if frame.frame_id == frame_id), None)

    def _frame_for_id(self, frame_id: str) -> FrameData | None:
        index = self._frame_index_for_id(frame_id)
        return self.frames[index] if index is not None else None

    def _timeline_item_for_frame_id(self, frame_id: str) -> QListWidgetItem | None:
        for index in range(self.timeline.count()):
            item = self.timeline.item(index)
            if str(item.data(Qt.ItemDataRole.UserRole)) == frame_id:
                return item
        return None

    def _select_timeline_frame_id(self, frame_id: str) -> None:
        for timeline in self.group_timeline_widgets.values() or [self.timeline]:
            item = next(
                (
                    timeline.item(index)
                    for index in range(timeline.count())
                    if str(timeline.item(index).data(Qt.ItemDataRole.UserRole)) == frame_id
                ),
                None,
            )
            if item is None:
                continue
            timeline.blockSignals(True)
            try:
                timeline.setCurrentItem(item)
            finally:
                timeline.blockSignals(False)

    def _timeline_row_changed(self, index: int) -> None:
        if self._timeline_drag_active:
            return
        self._request_timeline_frame(self._primary_timeline_group_id, index)

    def _apply_pending_timeline_frame(self) -> None:
        frame_id = self._pending_timeline_frame_id
        self._pending_timeline_frame_id = None
        index = self._frame_index_for_id(frame_id) if frame_id else None
        if index is not None:
            self.set_current_frame(index, update_timeline=True)

    def _set_timeline_drag_active(self) -> None:
        self._timeline_drag_active = True

    def _clear_timeline_drag_active(self) -> None:
        self._timeline_drag_active = False

    def _timeline_frame_move_requested(self, source_id: str, anchor_id: str, place_after: bool) -> None:
        if not getattr(self, "_rebuilding_timeline", False):
            self._move_frame_by_id(source_id, anchor_id, place_after)

    def _show_timeline_context_menu(self, position: QPoint) -> None:
        self._show_group_timeline_context_menu(self._primary_timeline_group_id, self.timeline, position)

    def _show_group_timeline_context_menu(
        self,
        group_id: str,
        timeline: TimelineListWidget,
        position: QPoint,
    ) -> None:
        if position.x() < 0 or position.x() + self.timeline_geometry.scroll_x >= self.timeline_geometry.content_width(len(self.frames)):
            return
        index = self.timeline_geometry.frame_index_at(position.x(), len(self.frames))
        if index < 0 or index >= len(self.frame_paths):
            return
        self._activate_layer_group(group_id, focus_timeline=False)
        menu = QMenu(timeline)
        copy_action = menu.addAction(
            "复制本组此帧笔迹",
            lambda: self.copy_layer_strokes(group_id, self.frames[index].frame_id),
        )
        copy_action.setEnabled(self._drawing_for_group(self._active_layer_group(), self.frames[index].frame_id) is not None)
        paste_action = menu.addAction(
            "粘贴笔迹到本组此帧",
            lambda: self.paste_layer_strokes(group_id, self.frames[index].frame_id),
        )
        paste_action.setEnabled(
            self._layer_stroke_clipboard is not None
            and self._layer_stroke_clipboard[0] == group_id
        )
        menu.addSeparator()
        exposure_menu = menu.addMenu("曝光拍数")
        for exposure in (1, 2, 3, 4, 6, 8):
            action = exposure_menu.addAction(f"{exposure} 拍")
            action.setCheckable(True)
            action.setChecked(self.frame_exposures[index] == exposure)
            action.triggered.connect(lambda _checked=False, value=exposure: self.set_frame_exposure(index, value))
        exposure_menu.addSeparator()
        exposure_menu.addAction("自定义...", lambda: self._prompt_frame_exposure(index))
        menu.exec(timeline.viewport().mapToGlobal(position))

    def copy_layer_strokes(self, group_id: str, frame_id: str) -> bool:
        group = next((candidate for candidate in self.layer_groups if candidate.group_id == group_id), None)
        if group is None:
            return False
        image = self._drawing_for_group(group, frame_id)
        if image is None or image.isNull():
            return False
        self._layer_stroke_clipboard = (group_id, image.copy())
        return True

    def paste_layer_strokes(self, group_id: str, frame_id: str) -> bool:
        if self._layer_stroke_clipboard is None or self._layer_stroke_clipboard[0] != group_id:
            return False
        group = next((candidate for candidate in self.layer_groups if candidate.group_id == group_id), None)
        frame = self._frame_for_id(frame_id)
        if group is None or frame is None:
            return False
        after = self._layer_stroke_clipboard[1].copy()
        before = self._drawing_for_group(group, frame_id)
        if before is None or before.isNull():
            before = self._new_blank_drawing_image(after.size())
        else:
            before = before.copy()
        operation = self._drawing_operation_from_images("paste", before, after)
        if operation is None:
            return True

        history = group.histories.get(frame_id)
        if history is None:
            history = FrameDrawingHistory()
            group.histories[frame_id] = history
        group.drawings[frame_id] = after
        if group_id == self.active_layer_group_id:
            frame.drawing = after
            frame.history = history
            self._history_memory.commit(frame_id, history, operation, self._histories_by_id())
        else:
            history.commit(operation)
        if group_id == self.active_layer_group_id and frame_id == self.frames[self.current_index].frame_id:
            self.sync_drawing_layer_to_views()
        self._mark_group_frame_thumbnail_dirty(group_id, frame_id)
        return True

    def _prompt_frame_exposure(self, index: int) -> None:
        value, accepted = QInputDialog.getInt(
            self,
            "曝光拍数",
            "拍数",
            self.frame_exposures[index],
            1,
            99,
        )
        if accepted:
            self.set_frame_exposure(index, value)

    def _frame_records(self) -> list[FrameData]:
        return list(self.frames)

    def _restore_frame_records(self, records: list[FrameData], current_index: int) -> None:
        if not records or len({frame.frame_id for frame in records}) != len(records):
            return
        selected_frame_id = records[max(0, min(current_index, len(records) - 1))].frame_id
        self.frames = list(records)
        self._history_memory.reindex(self._histories_by_id())
        self.current_index = max(0, min(current_index, len(records) - 1))
        if not self._sync_timeline_from_frames(selected_frame_id):
            self._load_timeline(selected_frame_id)
        self.set_current_frame(self.current_index, update_timeline=True, ensure_visible=True)

    def _reorder_frames(self, order: list[int], *, current_old_index: int | None = None) -> None:
        if sorted(order) != list(range(len(self.frame_paths))):
            return
        records = self._frame_records()
        old_current = self.current_index if current_old_index is None else current_old_index
        self._restore_frame_records([records[index] for index in order], order.index(old_current))

    def _move_frame_by_id(self, source_id: str, anchor_id: str = "", place_after: bool = True) -> bool:
        """Atomically move one FrameData record using only stable frame ids."""
        records_by_id = {frame.frame_id: frame for frame in self.frames}
        if len(records_by_id) != len(self.frames) or source_id not in records_by_id:
            return False
        if anchor_id and anchor_id not in records_by_id:
            return False
        if source_id == anchor_id:
            return False

        records = list(self.frames)
        source = records_by_id[source_id]
        records.remove(source)
        if anchor_id:
            anchor_index = next(index for index, frame in enumerate(records) if frame.frame_id == anchor_id)
            insert_at = anchor_index + (1 if place_after else 0)
        else:
            insert_at = len(records)
        records.insert(insert_at, source)
        if [frame.frame_id for frame in records] == [frame.frame_id for frame in self.frames]:
            return False
        self._restore_frame_records(records, insert_at)
        return True

    def set_frame_exposure(self, index: int, exposure: int) -> None:
        if index < 0 or index >= len(self.frame_exposures):
            return
        self.frames[index].exposure = max(1, min(99, int(exposure)))
        item = self.timeline.item(index)
        if item is not None:
            suffix = f" x{self.frames[index].exposure}" if self.frames[index].exposure > 1 else ""
            item.setText(f"{index + 1}{suffix}")
        if index == self.current_index:
            self._playback_exposure_tick = 0
            self.set_current_frame(index)

    def set_thumbnail_display_mode(self, *_args) -> None:
        self._pending_thumbnail_display_mode = self.thumbnail_display_combo.currentData()
        self._apply_pending_thumbnail_display_mode()

    def set_timeline_zoom(self, *_args) -> None:
        if not hasattr(self, "timeline_zoom_slider"):
            return
        percent = int(self.timeline_zoom_slider.value())
        self.timeline_zoom_value_label.setText(f"{percent}%")
        old_screen_center = self.timeline_geometry.frame_center(self.current_index)
        self.timeline_zoom = max(0.5, min(2.0, percent / 100.0))
        self.timeline_geometry.set_zoom_percent(percent)
        icon_size = self.timeline_geometry.thumbnail_size()
        grid_size = QSize(self.timeline_geometry.frame_width(), self.timeline_geometry.row_height())
        timelines = list(getattr(self, "group_timeline_widgets", {}).values()) or [self.timeline]
        for timeline in timelines:
            timeline.setIconSize(icon_size)
            timeline.setGridSize(grid_size)
            timeline.setFixedHeight(self.timeline_geometry.row_height())
            for index in range(timeline.count()):
                timeline.item(index).setSizeHint(grid_size)
            timeline.doItemsLayout()
            timeline.viewport().update()
        for row in getattr(self, "group_timeline_row_hosts", {}).values():
            row.setFixedHeight(self.timeline_geometry.row_height())
        for check in getattr(self, "group_timeline_visibility_checks", {}).values():
            check.setFixedHeight(self.timeline_geometry.row_height())
        paper_row = getattr(self, "timeline_paper_row_host", None)
        if paper_row is not None:
            paper_row.setFixedHeight(self.timeline_geometry.row_height())
        paper_check = getattr(self, "timeline_paper_visibility_checkbox", None)
        if paper_check is not None:
            paper_check.setFixedHeight(self.timeline_geometry.row_height())
        paper_track = getattr(self, "timeline_paper_track", None)
        if paper_track is not None:
            paper_track.update()
        self._settings.setValue("timeline_zoom", percent)
        self._update_timeline_scroll_range()
        new_content_center = self.current_index * self.timeline_geometry.frame_width() + self.timeline_geometry.frame_width() / 2
        target_scroll = round(new_content_center - old_screen_center)
        if hasattr(self, "timeline_horizontal_scrollbar"):
            self.timeline_horizontal_scrollbar.setValue(max(0, target_scroll))
        if hasattr(self, "timeline_ruler"):
            self.timeline_ruler.update()
        self._schedule_visible_thumbnail_refresh(delay_ms=16)


    def _apply_pending_thumbnail_display_mode(self) -> None:
        if self._pending_thumbnail_display_mode is None:
            return
        self.thumbnail_display_mode = self._pending_thumbnail_display_mode
        self._pending_thumbnail_display_mode = None
        self._thumbnail_generation += 1
        self._settings.setValue("thumbnail_display_mode", self.thumbnail_display_mode)
        first, last = self.timeline_geometry.visible_frame_range(self.timeline.viewport().width(), len(self.frames))
        groups_by_id = {group.group_id: group for group in self.layer_groups}
        for group_id, timeline in getattr(self, "group_timeline_widgets", {}).items():
            group = groups_by_id.get(group_id)
            if group is None:
                continue
            for index in range(max(0, first), min(timeline.count() - 1, last) + 1):
                frame_id = str(timeline.item(index).data(Qt.ItemDataRole.UserRole))
                cached = self._group_thumbnail_cache.get(self._group_thumbnail_cache_key(group, frame_id))
                timeline.item(index).setIcon(QIcon(cached if cached is not None else self._thumbnail_placeholder()))
        self._schedule_visible_thumbnail_refresh(delay_ms=16)

    def generate_frame_thumbnail(self, frame_id: str, display_mode: str | None = None) -> QPixmap:
        """Create one centered thumbnail without canvas-only helper overlays."""
        start = perf_counter()
        mode = display_mode or self.thumbnail_display_mode
        frame = self._frame_for_id(frame_id)
        if frame is None:
            return self._thumbnail_placeholder()
        icon_size = self.timeline_geometry.thumbnail_size()
        version = self._thumbnail_content_versions.get((frame.frame_id, "__composite__"), 0)
        cache_key = (frame.frame_id, "__composite__", mode, icon_size.width(), icon_size.height(), version)
        cached = self._thumbnail_cache.get(cache_key)
        if cached is not None:
            return cached

        reference = self._reference_image_for_thumbnail(frame.frame_id)
        drawing = self._composited_visible_drawing(frame.frame_id)
        aspect_source = reference if not reference.isNull() else drawing
        if aspect_source is None or aspect_source.isNull():
            thumbnail = self._thumbnail_placeholder()
            frame.thumbnail_cache[mode] = thumbnail
            frame.thumbnail_dirty = False
            self._thumbnail_cache[cache_key] = thumbnail
            self._thumbnail_dirty.discard(frame.frame_id)
            return thumbnail

        thumbnail_image = QImage(icon_size, QImage.Format.Format_ARGB32_Premultiplied)
        thumbnail_image.fill(
            Qt.GlobalColor.white
            if self.paper_visible and mode in ("drawing", "composite")
            else Qt.GlobalColor.transparent
        )
        target_size = aspect_source.size().scaled(icon_size, Qt.AspectRatioMode.KeepAspectRatio)
        target_rect = QRect(
            (icon_size.width() - target_size.width()) // 2,
            (icon_size.height() - target_size.height()) // 2,
            target_size.width(),
            target_size.height(),
        )
        painter = QPainter(thumbnail_image)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if mode in ("reference", "composite"):
            if not reference.isNull():
                painter.drawImage(target_rect, reference)
        if mode != "reference":
            if drawing is not None and not drawing.isNull():
                painter.drawImage(target_rect, drawing)
        painter.end()
        thumbnail = QPixmap.fromImage(thumbnail_image)
        frame.thumbnail_cache[mode] = thumbnail
        frame.thumbnail_dirty = False
        self._thumbnail_cache[cache_key] = thumbnail
        self._thumbnail_dirty.discard(frame.frame_id)
        elapsed_ms = (perf_counter() - start) * 1000
        if elapsed_ms >= 16.0:
            print(f"[thumbnail-generate] frame_id={frame.frame_id} mode={mode} total={elapsed_ms:.2f}ms")
        return thumbnail

    def _reference_image_for_thumbnail(self, frame_id: str) -> QImage:
        frame = self._frame_for_id(frame_id)
        if frame is None:
            return QImage()
        if frame.thumbnail_reference is not None and not frame.thumbnail_reference.isNull():
            return frame.thumbnail_reference
        if frame.reference_image is not None:
            image = frame.reference_image.copy()
        else:
            image = QImage(str(frame.path)).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        frame.thumbnail_reference = image
        return image

    def _drawing_layer_for_thumbnail(self, frame_id: str, canvas_size: QSize) -> QImage:
        drawing = self._composited_visible_drawing(frame_id, canvas_size)
        return drawing if drawing is not None else QImage()

    def _centered_thumbnail_pixmap(self, image: QImage) -> QPixmap:
        icon_size = self.timeline_geometry.thumbnail_size()
        scaled = image.scaled(
            icon_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        pixmap = QPixmap(icon_size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.drawImage(
            (icon_size.width() - scaled.width()) // 2,
            (icon_size.height() - scaled.height()) // 2,
            scaled,
        )
        painter.end()
        return pixmap

    def _mark_frame_thumbnail_dirty(self, frame_id: str) -> None:
        self._mark_group_frame_thumbnail_dirty(self.active_layer_group_id, frame_id)

    def _mark_group_frame_thumbnail_dirty(self, group_id: str, frame_id: str) -> None:
        frame = self._frame_for_id(frame_id)
        if frame is None:
            return
        frame.thumbnail_dirty = True
        self._thumbnail_dirty.add(frame.frame_id)
        for source in (group_id, "__composite__"):
            version_key = (frame_id, source)
            self._thumbnail_content_versions[version_key] = self._thumbnail_content_versions.get(version_key, 0) + 1
        for mode in ("reference", "drawing", "composite"):
            frame.thumbnail_cache.pop(mode, None)
        self._thumbnail_cache = {key: value for key, value in self._thumbnail_cache.items() if key[0] != frame_id}
        self._group_thumbnail_cache = {
            key: value
            for key, value in self._group_thumbnail_cache.items()
            if not (key[0] == frame_id and key[1] == group_id)
        }
        self._schedule_visible_thumbnail_refresh(delay_ms=80)

    def _invalidate_composite_thumbnails(self) -> None:
        for frame in self.frames:
            key = (frame.frame_id, "__composite__")
            self._thumbnail_content_versions[key] = self._thumbnail_content_versions.get(key, 0) + 1
            frame.thumbnail_cache.pop("composite", None)
        self._thumbnail_cache = {
            key: value for key, value in self._thumbnail_cache.items() if len(key) < 2 or key[1] != "__composite__"
        }

    def _mark_all_thumbnails_dirty(self) -> None:
        for frame in self.frames:
            frame.thumbnail_dirty = True
            frame.thumbnail_cache.clear()
        self._thumbnail_dirty.update(frame.frame_id for frame in self.frames)
        self._thumbnail_cache.clear()
        self._group_thumbnail_cache.clear()
        self._thumbnail_content_versions.clear()
        self._schedule_visible_thumbnail_refresh()

    def _schedule_visible_thumbnail_refresh(self, *_args, delay_ms: int = 0) -> None:
        delay_ms = max(16, delay_ms)
        if self._thumbnail_refresh_timer.isActive():
            if delay_ms > 0:
                self._thumbnail_refresh_timer.start(delay_ms)
            return
        self._thumbnail_refresh_timer.start(delay_ms)

    def _refresh_visible_thumbnails(self) -> None:
        idle_ms = (perf_counter() - self._last_frame_switch_at) * 1000
        if idle_ms < 120:
            self._schedule_visible_thumbnail_refresh(delay_ms=max(16, int(120 - idle_ms)))
            return
        refresh_start = perf_counter()
        refreshed = 0
        first, last = self.timeline_geometry.visible_frame_range(self.timeline.viewport().width(), len(self.frames))
        visible_groups = self._visible_timeline_groups()
        pending_frame_ids: list[str] = []
        for index in range(max(0, first), min(len(self.frames) - 1, last) + 1):
            frame_id = self.frames[index].frame_id
            if any(
                self._group_thumbnail_cache_key(group, frame_id) not in self._group_thumbnail_cache
                for group in visible_groups
            ):
                pending_frame_ids.append(frame_id)
        generation = self._thumbnail_generation
        for frame_id in pending_frame_ids[:2]:
            if generation != self._thumbnail_generation:
                return
            for group in visible_groups:
                timeline = self.group_timeline_widgets.get(group.group_id)
                item = next(
                    (
                        timeline.item(index)
                        for index in range(timeline.count())
                        if str(timeline.item(index).data(Qt.ItemDataRole.UserRole)) == frame_id
                    ),
                    None,
                ) if timeline is not None else None
                if item is not None:
                    item.setIcon(QIcon(self._group_frame_thumbnail(group, frame_id)))
            refreshed += 1
        if len(pending_frame_ids) > refreshed:
            self._schedule_visible_thumbnail_refresh(delay_ms=16)
        total_ms = (perf_counter() - refresh_start) * 1000
        if total_ms >= 16.0:
            print(f"[thumbnail-refresh] visible_items={refreshed} total={total_ms:.2f}ms")

    def _refresh_frame_thumbnail_if_visible(self, frame_id: str) -> None:
        index = self._frame_index_for_id(frame_id)
        if index is None:
            return
        first, last = self.timeline_geometry.visible_frame_range(self.timeline.viewport().width(), len(self.frames))
        if first <= index <= last:
            self._refresh_group_timeline_icons(frame_id)

    def _update_timeline_item_thumbnail(self, frame_id: str, generation: int) -> None:
        if generation != self._thumbnail_generation:
            return
        item = self._timeline_item_for_frame_id(frame_id)
        if item is None or str(item.data(Qt.ItemDataRole.UserRole)) != frame_id:
            return
        # Keep the legacy per-frame cache warm, but never put its composited
        # result into a layer-group row.  ``self.timeline`` is reused as the
        # first (topmost) group timeline, so the old asynchronous refresh used
        # to overwrite that group's isolated icon with all visible groups.
        fallback = self.generate_frame_thumbnail(frame_id, self.thumbnail_display_mode)
        primary_group = next(
            (group for group in self.layer_groups if group.group_id == self._primary_timeline_group_id),
            None,
        )
        item.setIcon(QIcon(self._group_frame_thumbnail(primary_group, frame_id) if primary_group else fallback))

    def eventFilter(self, watched, event):  # type: ignore[override]
        if (
            event.type() == QEvent.Type.Wheel
            and isinstance(watched, QWidget)
            and bool(watched.property("timelineWheelTarget"))
        ):
            return self._scroll_timeline_tracks_vertically(event)

        timeline = getattr(self, "timeline", None)
        timeline_viewports = {
            candidate.viewport()
            for candidate in getattr(self, "group_timeline_widgets", {}).values()
        }
        if timeline is not None:
            timeline_viewports.add(timeline.viewport())
        if watched in timeline_viewports and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._update_timeline_scroll_range()
            self._schedule_visible_thumbnail_refresh()

        if (
            event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and isinstance(watched, QWidget)
            and (watched is self or self.isAncestorOf(watched))
            and self._dispatch_shortcut(event)
        ):
            return True
        return super().eventFilter(watched, event)

    def import_gif(self) -> None:
        gif_path, _ = QFileDialog.getOpenFileName(
            self,
            "导入 GIF",
            str(APP_DIR),
            "GIF Files (*.gif);;All Files (*)",
        )
        if not gif_path:
            return

        self.current_gif_path = Path(gif_path)
        self.import_step_slider.blockSignals(True)
        self.import_step_slider.setValue(1)
        self.import_step_slider.setEnabled(True)
        self.import_step_slider.blockSignals(False)
        self.import_step_label.setText("1")
        self.load_current_gif(step=1)

    def set_import_step(self, value: int) -> None:
        self.import_step_label.setText(str(value))
        if self.current_gif_path is None:
            return
        self.load_current_gif(step=value)

    def load_current_gif(self, step: int) -> None:
        if self.current_gif_path is None:
            return

        try:
            frames = load_gif_frames(self.current_gif_path, step=step)
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", f"读取 GIF 失败：\n{exc}")
            return

        if not frames:
            self.frame_label.setText("GIF 没有可用帧")
            return

        self.play_timer.stop()
        self.play_button.setChecked(False)
        self._set_playback_drawing_blocked(False)
        self._refresh_onion_skin_visibility()
        self._update_play_icon()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = APP_DIR / "work" / "imported_gif_frames" / f"{self.current_gif_path.stem}_step{step}_{timestamp}"
        try:
            imported_paths = export_png_sequence(frames, output_dir)
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", f"导出 PNG 序列失败：\n{exc}")
            return

        self.frames = [
            FrameData(
                frame_id=uuid4().hex,
                path=path,
                duration=frame.duration if frame.duration > 0 else 120,
            )
            for path, frame in zip(imported_paths, frames)
        ]
        self.playback_range_start = 0
        self.playback_range_end = max(0, len(self.frames) - 1)
        self._reset_layer_groups()
        self.current_index = 0
        self.current_project_path = None
        self._history_memory.reset()
        self._pending_stroke_before.clear()

        self._load_timeline()
        self.timeline.setCurrentRow(0)

    def save_project(self) -> None:
        if not self.frame_paths:
            return
        if self.current_project_path is None:
            self.save_project_as()
            return
        self._write_project(self.current_project_path)

    def save_project_as(self) -> None:
        if not self.frame_paths:
            return
        default_path = self.current_project_path or (APP_DIR / "work" / f"tracing_project{PROJECT_SUFFIX}")
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "保存工程",
            str(default_path),
            f"Tracing Project (*{PROJECT_SUFFIX})",
        )
        if not filename:
            return
        project_path = Path(filename)
        if project_path.suffix.lower() != PROJECT_SUFFIX:
            project_path = project_path.with_suffix(PROJECT_SUFFIX)
        if self._write_project(project_path):
            self.current_project_path = project_path

    def _write_project(self, project_path: Path) -> bool:
        try:
            self._store_active_layer_group()
            project_path.parent.mkdir(parents=True, exist_ok=True)
            manifest: dict[str, object] = {
                "version": 4,
                "frame_durations": self.frame_durations,
                "frame_exposures": self.frame_exposures,
                "frame_ids": [frame.frame_id for frame in self.frames],
                "practice_scale": self.practice_scale,
                "current_index": self.current_index,
                "playback_range": [self.playback_range_start, self.playback_range_end],
                "onion_loop": self.onion_loop_checkbox.isChecked(),
                "paper_visible": self.paper_visible,
                "reference_frames": [],
                "drawing_layers": {},
                "active_layer_group_id": self.active_layer_group_id,
                "layer_groups": [],
            }
            with zipfile.ZipFile(project_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                reference_entries: list[str] = []
                drawing_entries: dict[str, str] = {}
                for index, frame in enumerate(self.frames):
                    reference = self._reference_image_for_thumbnail(frame.frame_id)
                    if reference.isNull():
                        raise ValueError(f"Unable to read reference frame: {frame.path}")
                    reference_entry = f"reference/frame_{index:04d}.png"
                    archive.writestr(reference_entry, self._qimage_to_png_bytes(reference))
                    reference_entries.append(reference_entry)

                    drawing = self.drawing_layers.get(index)
                    if drawing is not None and not drawing.isNull():
                        drawing_entry = f"drawing/frame_{index:04d}.png"
                        archive.writestr(drawing_entry, self._qimage_to_png_bytes(drawing))
                        drawing_entries[str(index)] = drawing_entry
                manifest["reference_frames"] = reference_entries
                manifest["drawing_layers"] = drawing_entries
                group_entries: list[dict[str, object]] = []
                for group_index, group in enumerate(self.layer_groups):
                    entries: dict[str, str] = {}
                    for frame_index, frame in enumerate(self.frames):
                        drawing = group.drawings.get(frame.frame_id)
                        if drawing is None or drawing.isNull():
                            continue
                        entry = f"layers/group_{group_index:04d}/frame_{frame_index:04d}.png"
                        archive.writestr(entry, self._qimage_to_png_bytes(drawing))
                        entries[str(frame_index)] = entry
                    group_entries.append({
                        "id": group.group_id,
                        "name": group.name,
                        "visible": group.visible,
                        "drawings": entries,
                    })
                manifest["layer_groups"] = group_entries
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        except Exception as exc:
            QMessageBox.critical(self, "保存工程失败", str(exc))
            return False
        return True

    def open_project(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "打开工程",
            str(APP_DIR / "work"),
            f"Tracing Project (*{PROJECT_SUFFIX})",
        )
        if not filename:
            return
        project_path = Path(filename)
        try:
            with zipfile.ZipFile(project_path, "r") as archive:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                if manifest.get("version") not in (1, 2, 3, 4):
                    raise ValueError("不支持的工程版本。")
                reference_entries = manifest.get("reference_frames")
                if not isinstance(reference_entries, list) or not reference_entries:
                    raise ValueError("工程中没有参考帧。")

                restored_paths: list[Path] = []
                restored_references: dict[int, QImage] = {}
                for index, entry in enumerate(reference_entries):
                    if not isinstance(entry, str) or not entry.startswith("reference/"):
                        raise ValueError("工程参考帧路径无效。")
                    image = QImage()
                    if not image.loadFromData(archive.read(entry), "PNG") or image.isNull():
                        raise ValueError(f"无法读取工程参考帧：{entry}")
                    restored_references[index] = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                    restored_paths.append(Path(f"{project_path.stem}_frame_{index:04d}.png"))

                drawing_layers: dict[int, QImage] = {}
                raw_drawing_entries = manifest.get("drawing_layers", {})
                if isinstance(raw_drawing_entries, dict):
                    for raw_index, entry in raw_drawing_entries.items():
                        index = int(raw_index)
                        if index < 0 or index >= len(restored_paths) or not isinstance(entry, str) or not entry.startswith("drawing/"):
                            continue
                        image = QImage()
                        if image.loadFromData(archive.read(entry), "PNG") and not image.isNull():
                            drawing_layers[index] = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)

                restored_groups: list[tuple[str, str, bool, dict[int, QImage]]] = []
                raw_groups = manifest.get("layer_groups", [])
                if isinstance(raw_groups, list):
                    for group_index, raw_group in enumerate(raw_groups):
                        if not isinstance(raw_group, dict):
                            continue
                        restored_drawings: dict[int, QImage] = {}
                        raw_entries = raw_group.get("drawings", {})
                        if isinstance(raw_entries, dict):
                            for raw_index, entry in raw_entries.items():
                                index = int(raw_index)
                                if index < 0 or index >= len(restored_paths) or not isinstance(entry, str) or not entry.startswith("layers/"):
                                    continue
                                image = QImage()
                                if image.loadFromData(archive.read(entry), "PNG") and not image.isNull():
                                    restored_drawings[index] = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                        restored_groups.append((
                            str(raw_group.get("id") or uuid4().hex),
                            str(raw_group.get("name") or f"图层组 {group_index + 1}"),
                            bool(raw_group.get("visible", True)),
                            restored_drawings,
                        ))

                raw_durations = manifest.get("frame_durations", [])
                durations = [max(1, int(value)) for value in raw_durations] if isinstance(raw_durations, list) else []
                if len(durations) != len(restored_paths):
                    durations = [120 for _ in restored_paths]
                raw_exposures = manifest.get("frame_exposures", [])
                exposures = [max(1, min(99, int(value))) for value in raw_exposures] if isinstance(raw_exposures, list) else []
                if len(exposures) != len(restored_paths):
                    exposures = [1 for _ in restored_paths]
                saved_scale = int(manifest.get("practice_scale", 3))
                saved_scale = saved_scale if saved_scale in (1, 2, 3, 4) else 3
                saved_index = max(0, min(int(manifest.get("current_index", 0)), len(restored_paths) - 1))
                raw_playback_range = manifest.get("playback_range", [0, len(restored_paths) - 1])
                if isinstance(raw_playback_range, list) and len(raw_playback_range) == 2:
                    saved_range_start = max(0, min(int(raw_playback_range[0]), len(restored_paths) - 1))
                    saved_range_end = max(saved_range_start, min(int(raw_playback_range[1]), len(restored_paths) - 1))
                else:
                    saved_range_start, saved_range_end = 0, len(restored_paths) - 1
                saved_onion_loop = bool(manifest.get("onion_loop", False))
                saved_paper_visible = bool(manifest.get("paper_visible", True))
        except Exception as exc:
            QMessageBox.critical(self, "打开工程失败", str(exc))
            return

        self.play_timer.stop()
        self.play_button.setChecked(False)
        self._set_playback_drawing_blocked(False)
        self.current_gif_path = None
        self.current_project_path = project_path
        self.import_step_slider.setEnabled(False)
        saved_ids = manifest.get("frame_ids", [])
        self.frames = [
            FrameData(
                frame_id=str(saved_ids[index]) if isinstance(saved_ids, list) and index < len(saved_ids) else uuid4().hex,
                path=path,
                duration=durations[index],
                exposure=exposures[index],
                reference_image=restored_references.get(index),
                drawing=drawing_layers.get(index),
            )
            for index, path in enumerate(restored_paths)
        ]
        if restored_groups:
            self.layer_groups = [
                LayerGroup(
                    group_id,
                    name,
                    visible,
                    {self.frames[index].frame_id: image for index, image in drawings.items()},
                )
                for group_id, name, visible, drawings in restored_groups
            ]
            requested_active = str(manifest.get("active_layer_group_id", ""))
            active = next((group for group in self.layer_groups if group.group_id == requested_active), self.layer_groups[0])
            self.active_layer_group_id = active.group_id
            for frame in self.frames:
                frame.drawing = active.drawings.get(frame.frame_id)
        else:
            self.layer_groups = [LayerGroup(uuid4().hex, "图层组 1")]
            self.active_layer_group_id = self.layer_groups[0].group_id
            self._store_active_layer_group()
        self._history_memory.reset()
        self._pending_stroke_before.clear()
        self._drawing_operation_context.clear()
        self.playback_range_start = saved_range_start
        self.playback_range_end = saved_range_end
        self.current_index = saved_index
        self.practice_scale_combo.blockSignals(True)
        self.practice_scale_combo.setCurrentIndex(self.practice_scale_combo.findData(saved_scale))
        self.practice_scale_combo.blockSignals(False)
        self.practice_scale = saved_scale
        self.onion_loop_checkbox.blockSignals(True)
        self.onion_loop_checkbox.setChecked(saved_onion_loop)
        self.onion_loop_checkbox.blockSignals(False)
        self.set_paper_visible(saved_paper_visible)
        self._rebuild_layer_group_list()
        self._load_timeline()
        self.set_current_frame(saved_index, update_timeline=True)

    def export_animated_gif(self) -> None:
        if not self.frame_paths:
            QMessageBox.information(self, "导出 GIF", "当前没有可导出的帧。")
            return
        default_path = APP_DIR / "work" / "tracing_export.gif"
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "导出 GIF",
            str(default_path),
            "GIF Animation (*.gif)",
        )
        if not filename:
            return
        output_path = Path(filename)
        if output_path.suffix.lower() != ".gif":
            output_path = output_path.with_suffix(".gif")
        try:
            from PIL import Image

            pil_frames = []
            for index in range(len(self.frame_paths)):
                image = self._drawing_canvas_export_frame(index)
                frame = Image.open(io.BytesIO(self._qimage_to_png_bytes(image))).convert(
                    "RGB" if self.paper_visible else "RGBA"
                )
                pil_frames.extend(frame.copy() for _ in range(self.frame_exposures[index]))
            if not pil_frames:
                raise ValueError("没有可导出的帧。")
            duration = max(1, round(1000 / self.export_fps_slider.value()))
            durations = [duration for _ in pil_frames]
            pil_frames[0].save(
                output_path,
                format="GIF",
                save_all=True,
                append_images=pil_frames[1:],
                duration=durations,
                loop=0,
                disposal=2,
            )
        except Exception as exc:
            QMessageBox.critical(self, "导出 GIF 失败", str(exc))
            return
        QMessageBox.information(self, "导出 GIF", f"GIF 已导出：\n{output_path}")

    @staticmethod
    def _qimage_to_png_bytes(image: QImage) -> bytes:
        data = QByteArray()
        buffer = QBuffer(data)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(buffer, "PNG"):
            raise ValueError("无法编码 PNG 图像。")
        buffer.close()
        return bytes(data)

    def _drawing_canvas_export_frame(self, frame_index: int) -> QImage:
        frame_id = self.frames[frame_index].frame_id
        reference = self._reference_image_for_thumbnail(frame_id)
        if reference.isNull():
            raise ValueError(f"无法读取第 {frame_index + 1} 帧参考图。")
        canvas_size = QSize(reference.width() * self.practice_scale, reference.height() * self.practice_scale)
        return self._final_composited_drawing(frame_id, canvas_size)

    def set_current_frame(
        self,
        index: int,
        *,
        update_timeline: bool = False,
        ensure_visible: bool = False,
    ) -> bool:
        """Single entry point for frame selection and all dependent UI state."""
        if index < 0 or index >= len(self.frame_paths):
            return False

        switch_start = perf_counter()
        self._last_frame_switch_at = switch_start
        self.current_index = index
        if hasattr(self, "timeline_ruler"):
            self.timeline_ruler.update()
        for timeline in getattr(self, "group_timeline_widgets", {}).values():
            timeline.viewport().update()
        paper_track = getattr(self, "timeline_paper_track", None)
        if paper_track is not None:
            paper_track.update()
        onion_active = self.onion_skin_checkbox.isChecked() and not self.play_timer.isActive()
        previous_index = self._onion_neighbor_index(index, -1) if onion_active else None
        next_index = self._onion_neighbor_index(index, 1) if onion_active else None
        previous_pixmap = self._load_frame_pixmap(self.frames[previous_index].frame_id) if previous_index is not None else None
        current_pixmap = self._load_frame_pixmap(self.frames[index].frame_id)
        next_pixmap = self._load_frame_pixmap(self.frames[next_index].frame_id) if next_index is not None else None
        image_read_ms = (perf_counter() - switch_start) * 1000

        if current_pixmap is None or current_pixmap.isNull():
            return False

        self._update_practice_canvas_size(current_pixmap)

        for canvas, scale in (
            (self.overlay_canvas, self.practice_scale),
            (self.compare_reference_canvas, self.practice_scale),
            (self.reference_only_canvas, self._reference_only_scale()),
            (self.reference_float_canvas, self.practice_scale),
        ):
            if canvas.isVisible():
                canvas.set_content_scale(scale)

        for canvas in self.reference_canvases:
            if not canvas.isVisible():
                continue
            canvas.set_frame_layers(previous_pixmap, current_pixmap, next_pixmap)
        reference_update_ms = (perf_counter() - switch_start) * 1000 - image_read_ms

        drawing_start = perf_counter()
        self.sync_drawing_layer_to_views(visible_only=True)
        self._refresh_onion_skin_visibility()
        drawing_onion_update_ms = (perf_counter() - drawing_start) * 1000

        frame_id = self.frames[index].frame_id
        if update_timeline:
            self._select_timeline_frame_id(frame_id)

        if ensure_visible:
            self._ensure_timeline_frame_visible(index)

        self.frame_label.setText(f"帧 {index + 1} / {len(self.frame_paths)}")
        self._restart_playback_timer_if_active()
        self._frame_preload_timer.start(48)
        total_ms = (perf_counter() - switch_start) * 1000
        if total_ms >= 16.0:
            print(
                "[frame-switch] "
                f"index={index} image_read={image_read_ms:.2f}ms "
                f"reference_update={reference_update_ms:.2f}ms "
                f"drawing_onion_update={drawing_onion_update_ms:.2f}ms total={total_ms:.2f}ms"
            )
        return True

    def set_view_mode(self, index: int) -> None:
        start = perf_counter()
        self.view_stack.setCurrentIndex(index)
        if self.view_stack.currentWidget() is self.float_reference_view:
            self.reference_float_window.show()
            self.reference_float_window.raise_()
            self.reference_float_window.canvas.fit_to_view()
        if self.frame_paths:
            self.refresh_current_frame()
        total_ms = (perf_counter() - start) * 1000
        if total_ms >= 16.0:
            print(f"[view-switch] mode={index} total={total_ms:.2f}ms")

    def request_view_mode(self, index: int) -> None:
        self._pending_view_mode = index
        self._view_switch_timer.start(16)

    def _apply_pending_view_mode(self) -> None:
        index = self._pending_view_mode
        self._pending_view_mode = None
        if index is not None:
            self.set_view_mode(index)

    def set_practice_scale(self, *_args) -> None:
        self.practice_scale = self.practice_scale_combo.currentData()
        self._mark_all_thumbnails_dirty()
        self.refresh_current_frame()

    def refresh_current_frame(self, *_args) -> None:
        self.set_current_frame(self.current_index)

    def fit_current_view(self) -> None:
        current = self.view_stack.currentWidget()
        if current is self.overlay_canvas:
            self.overlay_canvas.fit_to_view()
        elif current is self.reference_only_canvas:
            self.reference_only_canvas.fit_to_view()
        elif current is self.trace_only_canvas:
            self.trace_only_canvas.fit_to_view()
        elif current is self.float_reference_view:
            self.float_trace_canvas.fit_to_view()
            self.reference_float_canvas.fit_to_view()
        elif isinstance(current, QSplitter):
            self.compare_reference_canvas.fit_to_view()
            self.compare_trace_canvas.fit_to_view()

    def toggle_settings_panel(self) -> None:
        self.settings_dock.setVisible(not self.settings_dock.isVisible())

    def set_grid_enabled(self, enabled: bool) -> None:
        for canvas in self.all_canvases:
            canvas.set_grid_enabled(enabled)

    def set_grid_size(self, *_args) -> None:
        size = self.grid_size_combo.currentData()
        for canvas in self.all_canvases:
            canvas.set_grid_size(size)

    def set_grid_color(self, *_args) -> None:
        color = self.grid_color_combo.currentData()
        for canvas in self.all_canvases:
            canvas.set_grid_color(color)

    def set_grid_opacity(self, *_args) -> None:
        opacity = self.grid_opacity_combo.currentData()
        for canvas in self.all_canvases:
            canvas.set_grid_opacity(opacity)

    def set_drawing_tool(self, *_args) -> None:
        tool = self.tool_combo.currentData()
        for canvas in self.drawing_canvases:
            canvas.set_drawing_tool(tool)
        self._update_brush_parameter_visibility()

    def _update_brush_parameter_visibility(self) -> None:
        tool = self.tool_combo.currentData()
        is_soft_brush = tool == "soft_brush"
        self.soft_brush_settings_group.setVisible(is_soft_brush)
        self.airbrush_settings_group.setVisible(
            is_soft_brush and self.soft_renderer_mode_combo.currentData() == "airbrush"
        )
        self.square_brush_settings_group.setVisible(tool == "square_brush")
        self.pencil_settings_group.setVisible(tool == "pencil")
        self.gradient_settings_group.setVisible(tool == "gradient")

    def set_square_brush_angle_mode(self, *_args) -> None:
        follow_path = bool(self.square_brush_angle_combo.currentData())
        for canvas in self.drawing_canvases:
            canvas.set_square_brush_angle_mode(follow_path)

    def set_brush_color(self, *_args) -> None:
        color = self.brush_color_combo.currentData()
        for canvas in self.drawing_canvases:
            canvas.set_brush_color(color)

    def set_brush_opacity(self, value: int) -> None:
        opacity = value / 100
        for canvas in self.drawing_canvases:
            canvas.set_brush_opacity(opacity)
        self.brush_opacity_label.setText(f"{value}%")

    def set_brush_hardness(self, value: int) -> None:
        for canvas in self.drawing_canvases:
            canvas.set_brush_hardness(value)
        self.brush_hardness_label.setText(f"{value}%")

    def set_soft_renderer_mode(self, *_args) -> None:
        mode = self.soft_renderer_mode_combo.currentData()
        for canvas in self.drawing_canvases:
            canvas.set_soft_renderer_mode(mode)
        self._update_brush_parameter_visibility()

    def set_soft_edge_gamma(self, value: float) -> None:
        for canvas in self.drawing_canvases:
            canvas.set_soft_edge_gamma(value)

    def set_soft_edge_gamma_from_slider(self, value: int) -> None:
        gamma = value / 100
        self.set_soft_edge_gamma(gamma)
        self.soft_edge_gamma_label.setText(f"{gamma:.2f}")

    def set_soft_flow(self, value: int) -> None:
        flow = value / 100
        for canvas in self.drawing_canvases:
            canvas.set_soft_flow(flow)
        self.soft_flow_label.setText(f"{value}%")

    def set_soft_pressure_size_enabled(self, enabled: bool) -> None:
        for canvas in self.drawing_canvases:
            canvas.set_soft_pressure_size_enabled(enabled)

    def set_soft_pressure_opacity_enabled(self, enabled: bool) -> None:
        for canvas in self.drawing_canvases:
            canvas.set_soft_pressure_opacity_enabled(enabled)

    def set_square_pressure_size_enabled(self, enabled: bool) -> None:
        for canvas in self.drawing_canvases:
            canvas.set_square_pressure_size_enabled(enabled)

    def set_square_pressure_size_strength(self, value: float) -> None:
        for canvas in self.drawing_canvases:
            canvas.set_square_pressure_size_strength(value)

    def set_square_pressure_size_strength_from_slider(self, value: int) -> None:
        strength = value / 100
        self.set_square_pressure_size_strength(strength)
        self.square_pressure_strength_label.setText(f"{value}%")

    def set_square_min_pressure_size(self, value: float) -> None:
        for canvas in self.drawing_canvases:
            canvas.set_square_min_pressure_size(value)

    def set_square_min_pressure_size_from_slider(self, value: int) -> None:
        minimum = value / 100
        self.set_square_min_pressure_size(minimum)
        self.square_min_pressure_size_label.setText(f"{value}%")

    def set_pencil_grain(self, value: int) -> None:
        for canvas in self.drawing_canvases:
            canvas.set_pencil_grain(value)
        self.pencil_grain_label.setText(f"{value}%")

    def set_pencil_density(self, value: int) -> None:
        for canvas in self.drawing_canvases:
            canvas.set_pencil_density(value)
        self.pencil_density_label.setText(f"{value}%")

    def set_pencil_pressure_size_enabled(self, enabled: bool) -> None:
        for canvas in self.drawing_canvases:
            canvas.set_pencil_pressure_size_enabled(enabled)

    def set_pencil_pressure_density_enabled(self, enabled: bool) -> None:
        for canvas in self.drawing_canvases:
            canvas.set_pencil_pressure_density_enabled(enabled)

    def set_pencil_tip_variation(self, value: int) -> None:
        for canvas in self.drawing_canvases:
            canvas.set_pencil_tip_variation(value)
        self.pencil_tip_variation_label.setText(f"{value}%")

    def set_gradient_end_color(self, *_args) -> None:
        color = self.gradient_end_color_combo.currentData()
        for canvas in self.drawing_canvases:
            canvas.set_gradient_end_color(color)

    def set_soft_debug_mode(self, *_args) -> None:
        mode = self.soft_debug_combo.currentData()
        for canvas in self.drawing_canvases:
            canvas.set_soft_debug_mode(mode)

    def set_brush_size(self, *_args) -> None:
        size = self.brush_size_combo.currentData()
        for canvas in self.drawing_canvases:
            canvas.set_brush_size(size)

    def begin_drawing_operation(self, image: QImage) -> None:
        """Capture the pre-change image until the current drawing operation commits."""
        if self._syncing_drawing:
            return
        source = self.sender()
        kind = str(getattr(source, "_drawing_tool", "stroke"))
        frame_id = self.frames[self.current_index].frame_id
        self._pending_stroke_before[frame_id] = (kind, image.copy())
        if source is not None:
            self._drawing_operation_context[source] = (self.active_layer_group_id, frame_id)

    def _drawing_commit_matches_active_context(self) -> bool:
        source = self.sender()
        if source is None:
            return True
        context = self._drawing_operation_context.pop(source, None)
        if context is None:
            return True
        current = (self.active_layer_group_id, self.frames[self.current_index].frame_id)
        if context == current and self._active_layer_group().visible:
            return True
        self._pending_stroke_before.pop(context[1], None)
        self.sync_drawing_layer_to_views()
        return False

    def update_current_drawing_layer(self, *args) -> None:
        if self._syncing_drawing:
            return
        if not self._drawing_commit_matches_active_context():
            return
        if len(args) == 4:
            kind, rect, before_patch, after_patch = args
            self._update_current_drawing_layer_patch(
                str(kind),
                QRect(rect),
                QImage(before_patch),
                QImage(after_patch),
            )
            return
        if len(args) != 1:
            return
        image = args[0]
        pending = self._pending_stroke_before.pop(self.frames[self.current_index].frame_id, None)
        after = image.copy()
        self.drawing_layers[self.current_index] = after
        if pending is not None:
            kind, before = pending
            self._commit_drawing_operation_from_images(kind, before, after)
        self.sync_drawing_layer_to_views()
        self._mark_frame_thumbnail_dirty(self.frames[self.current_index].frame_id)

    def _update_current_drawing_layer_patch(
        self,
        kind: str,
        rect: QRect,
        before_patch: QImage,
        after_patch: QImage,
    ) -> None:
        total_start = perf_counter()
        self._pending_stroke_before.pop(self.frames[self.current_index].frame_id, None)
        image = self._drawing_image_for_current_frame()
        target_rect = rect.intersected(image.rect())
        if target_rect.isEmpty():
            return

        apply_start = perf_counter()
        self._apply_patch_to_image(image, rect, after_patch)
        self.drawing_layers[self.current_index] = image
        apply_ms = (perf_counter() - apply_start) * 1000

        history_start = perf_counter()
        self._commit_drawing_operation(
            DrawingOperation(
                kind=kind,
                rect=QRect(rect),
                before_patch=before_patch.copy(),
                after_patch=after_patch.copy(),
            )
        )
        history_ms = (perf_counter() - history_start) * 1000

        refresh_start = perf_counter()
        self._sync_drawing_patch_to_visible_views(rect, after_patch, exclude=self.sender())
        refresh_ms = (perf_counter() - refresh_start) * 1000

        thumb_start = perf_counter()
        self._mark_frame_thumbnail_dirty(self.frames[self.current_index].frame_id)
        thumbnail_ms = (perf_counter() - thumb_start) * 1000
        total_ms = (perf_counter() - total_start) * 1000
        if total_ms >= 8.0:
            print(
                "[drawing-commit] "
                f"kind={kind} rect={target_rect.width()}x{target_rect.height()} "
                f"patch_apply={apply_ms:.2f}ms undo_record={history_ms:.2f}ms "
                f"canvas_refresh={refresh_ms:.2f}ms thumbnail_schedule={thumbnail_ms:.2f}ms "
                f"handler_total={total_ms:.2f}ms"
            )

    def clear_current_drawing_layer(self) -> None:
        current_image = self._drawing_image_for_current_frame().copy()
        blank = self._new_blank_drawing_image(self.current_drawing_size)
        self.drawing_layers[self.current_index] = blank
        self._pending_stroke_before.pop(self.frames[self.current_index].frame_id, None)
        self._commit_drawing_operation_from_images("clear", current_image, blank)
        self.sync_drawing_layer_to_views()
        self._mark_frame_thumbnail_dirty(self.frames[self.current_index].frame_id)

    def undo_current_frame_drawing(self) -> None:
        history = self.frame_histories.get(self.current_index)
        if history is None or not history.undo:
            return
        operation = history.undo.pop()
        self._apply_drawing_operation_patch(operation, operation.before_patch)
        history.redo.append(operation)
        self._pending_stroke_before.pop(self.frames[self.current_index].frame_id, None)
        self._sync_drawing_patch_to_visible_views(operation.rect, operation.before_patch)
        self._mark_frame_thumbnail_dirty(self.frames[self.current_index].frame_id)

    def redo_current_frame_drawing(self) -> None:
        history = self.frame_histories.get(self.current_index)
        if history is None or not history.redo:
            return
        operation = history.redo.pop()
        self._apply_drawing_operation_patch(operation, operation.after_patch)
        history.undo.append(operation)
        self._pending_stroke_before.pop(self.frames[self.current_index].frame_id, None)
        self._sync_drawing_patch_to_visible_views(operation.rect, operation.after_patch)
        self._mark_frame_thumbnail_dirty(self.frames[self.current_index].frame_id)

    def _commit_drawing_operation(self, operation: DrawingOperation) -> None:
        frame = self.frames[self.current_index]
        history = frame.history
        if history is None:
            history = FrameDrawingHistory()
            frame.history = history
        self._history_memory.commit(
            frame.frame_id,
            history,
            operation,
            self._histories_by_id(),
        )

    def _histories_by_id(self) -> dict[str, FrameDrawingHistory]:
        return {
            frame.frame_id: frame.history
            for frame in self.frames
            if frame.history is not None
        }

    def _commit_drawing_operation_from_images(self, kind: str, before: QImage, after: QImage) -> None:
        operation = self._drawing_operation_from_images(kind, before, after)
        if operation is not None:
            self._commit_drawing_operation(operation)

    @staticmethod
    def _drawing_operation_from_images(
        kind: str,
        before: QImage,
        after: QImage,
    ) -> DrawingOperation | None:
        before = before.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        after = after.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        rect = FrameViewerWindow._changed_image_rect(before, after)
        if rect.isNull():
            return None
        return DrawingOperation(
            kind=kind,
            rect=rect,
            before_patch=before.copy(rect),
            after_patch=after.copy(rect),
        )

    @staticmethod
    def _changed_image_rect(before: QImage, after: QImage) -> QRect:
        if before.isNull() or after.isNull() or before.size() != after.size():
            return after.rect()

        width = after.width()
        height = after.height()
        before_bits = before.constBits()
        after_bits = after.constBits()
        stride = after.bytesPerLine()
        left = width
        right = -1
        top = height
        bottom = -1
        for y in range(height):
            row_start = y * stride
            row_end = row_start + width * 4
            if before_bits[row_start:row_end] == after_bits[row_start:row_end]:
                continue
            top = min(top, y)
            bottom = y
            for x in range(width):
                pixel_start = row_start + x * 4
                pixel_end = pixel_start + 4
                if before_bits[pixel_start:pixel_end] != after_bits[pixel_start:pixel_end]:
                    left = min(left, x)
                    right = max(right, x)

        if right < left or bottom < top:
            return QRect()
        return QRect(left, top, right - left + 1, bottom - top + 1)

    def _apply_drawing_operation_patch(self, operation: DrawingOperation, patch: QImage) -> None:
        image = self._drawing_image_for_current_frame().copy()
        self._apply_patch_to_image(image, operation.rect, patch)
        self.drawing_layers[self.current_index] = image

    @staticmethod
    def _apply_patch_to_image(image: QImage, rect: QRect, patch: QImage) -> None:
        target_rect = rect.intersected(image.rect())
        if target_rect.isEmpty() or patch.isNull():
            return
        source_rect = QRect(
            target_rect.x() - rect.x(),
            target_rect.y() - rect.y(),
            target_rect.width(),
            target_rect.height(),
        )
        painter = QPainter(image)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.drawImage(target_rect.topLeft(), patch.copy(source_rect))
        painter.end()

    def _sync_drawing_patch_to_visible_views(self, rect: QRect, patch: QImage, *, exclude=None) -> None:
        self._syncing_drawing = True
        try:
            for canvas in self.drawing_canvases:
                if canvas is exclude or not canvas.isVisible():
                    continue
                canvas.apply_drawing_patch(rect, patch)
        finally:
            self._syncing_drawing = False

    def sync_drawing_layer_to_views(self, *, visible_only: bool = False) -> None:
        image = self._drawing_image_for_current_frame()
        active_group = self._active_layer_group()
        active_index = self.layer_groups.index(active_group)
        # The list is front-to-back: rows above the active group render above it.
        above = self._composite_group_range(self.layer_groups[:active_index], self.frames[self.current_index].frame_id)
        below = self._composite_group_range(self.layer_groups[active_index + 1 :], self.frames[self.current_index].frame_id)
        self._syncing_drawing = True
        try:
            for canvas in self.drawing_canvases:
                if visible_only and not canvas.isVisible():
                    continue
                canvas.set_drawing_image(image)
                canvas.set_drawing_group_images(below, above)
                canvas.set_drawing_layer_visible(active_group.visible)
                canvas.set_drawing_enabled(active_group.visible)
        finally:
            self._syncing_drawing = False
        self._refresh_drawing_onion_layers()

    def _drawing_for_group(self, group: LayerGroup, frame_id: str) -> QImage | None:
        if group.group_id == self.active_layer_group_id:
            frame = self._frame_for_id(frame_id)
            return frame.drawing if frame is not None else None
        return group.drawings.get(frame_id)

    def _composite_group_range(self, groups: list[LayerGroup], frame_id: str) -> QImage | None:
        visible = [group for group in groups if group.visible]
        if not visible:
            return None
        result = self._new_blank_drawing_image(self.current_drawing_size)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for group in reversed(visible):
            image = self._drawing_for_group(group, frame_id)
            if image is None or image.isNull():
                continue
            if image.size() != result.size():
                image = image.scaled(result.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            painter.drawImage(0, 0, image)
        painter.end()
        return result

    def _composited_visible_drawing(self, frame_id: str, size: QSize | None = None) -> QImage | None:
        target_size = QSize(size) if size is not None else QSize(self.current_drawing_size)
        if target_size.isEmpty():
            return None
        result = self._new_blank_drawing_image(target_size)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        drew_content = False
        for group in reversed(self.layer_groups):
            if not group.visible:
                continue
            image = self._drawing_for_group(group, frame_id)
            if image is None or image.isNull():
                continue
            if image.size() != target_size:
                image = image.scaled(target_size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            painter.drawImage(0, 0, image)
            drew_content = True
        painter.end()
        return result if drew_content else None

    def _final_composited_drawing(self, frame_id: str, size: QSize | None = None) -> QImage:
        target_size = QSize(size) if size is not None else QSize(self.current_drawing_size)
        target_size = target_size if not target_size.isEmpty() else QSize(1, 1)
        result = QImage(target_size, QImage.Format.Format_ARGB32_Premultiplied)
        result.fill(Qt.GlobalColor.white if self.paper_visible else Qt.GlobalColor.transparent)
        drawing = self._composited_visible_drawing(frame_id, target_size)
        if drawing is not None and not drawing.isNull():
            painter = QPainter(result)
            painter.drawImage(0, 0, drawing)
            painter.end()
        return result

    def export_current_drawing_png(self, path: str | Path) -> bool:
        if not self.frames:
            return False
        frame_id = self.frames[self.current_index].frame_id
        return self._final_composited_drawing(frame_id).save(str(path), "PNG")

    def set_onion_skin_enabled(self, enabled: bool) -> None:
        self._refresh_drawing_onion_layers()
        self._refresh_onion_skin_visibility()

    def set_onion_loop_enabled(self, enabled: bool) -> None:
        self.refresh_current_frame()

    def _onion_neighbor_index(self, index: int, offset: int) -> int | None:
        count = len(self.frame_paths)
        if count <= 1:
            return None
        candidate = index + offset
        if 0 <= candidate < count:
            return candidate
        if self.onion_loop_checkbox.isChecked():
            return candidate % count
        return None

    def set_onion_skin_opacity(self, value: int) -> None:
        for canvas in self.reference_canvases:
            canvas.set_frame_opacity(value / 100)
        for canvas in self.drawing_canvases:
            canvas.set_drawing_onion_opacity(value / 100)
        self.onion_opacity_label.setText(f"{value}%")

    def set_previous_onion_skin_color(self, *_args) -> None:
        for canvas in self.reference_canvases:
            canvas.set_previous_onion_skin_color(self.previous_onion_color_combo.currentData())
        for canvas in self.drawing_canvases:
            canvas.set_drawing_previous_onion_color(self.previous_onion_color_combo.currentData())

    def set_next_onion_skin_color(self, *_args) -> None:
        for canvas in self.reference_canvases:
            canvas.set_next_onion_skin_color(self.next_onion_color_combo.currentData())
        for canvas in self.drawing_canvases:
            canvas.set_drawing_next_onion_color(self.next_onion_color_combo.currentData())

    def _refresh_onion_skin_visibility(self) -> None:
        enabled = self.onion_skin_checkbox.isChecked() and not self.play_timer.isActive()
        for canvas in self.reference_canvases:
            canvas.set_onion_skin_enabled(enabled)
        for canvas in self.drawing_canvases:
            canvas.set_drawing_onion_skin_enabled(enabled)

    def _refresh_drawing_onion_layers(self) -> None:
        if self.play_timer.isActive():
            self._refresh_onion_skin_visibility()
            return

        previous = None
        next_image = None
        if self.onion_skin_checkbox.isChecked():
            previous_index = self._onion_neighbor_index(self.current_index, -1)
            next_index = self._onion_neighbor_index(self.current_index, 1)
            previous = self._drawing_onion_image_for_frame(previous_index) if previous_index is not None else None
            next_image = self._drawing_onion_image_for_frame(next_index) if next_index is not None else None
        for canvas in self.drawing_canvases:
            if not canvas.isVisible():
                continue
            canvas.set_drawing_onion_layers(previous, next_image)
        self._refresh_onion_skin_visibility()

    def _drawing_onion_image_for_frame(self, frame_index: int) -> QImage | None:
        if frame_index < 0 or frame_index >= len(self.frame_paths):
            return None
        group = self._active_layer_group()
        if not group.visible:
            return None
        image = self._drawing_for_group(group, self.frames[frame_index].frame_id)
        if image is None or image.isNull():
            return None
        if image.size() == self.current_drawing_size:
            return image
        return image.scaled(
            self.current_drawing_size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _update_practice_canvas_size(self, reference_pixmap: QPixmap) -> None:
        width = reference_pixmap.width() * self.practice_scale
        height = reference_pixmap.height() * self.practice_scale
        self.current_drawing_size = QSize(width, height)
        for canvas in (self.compare_trace_canvas, self.trace_only_canvas, self.float_trace_canvas):
            if canvas.isVisible():
                canvas.set_canvas_size(width, height, self.practice_scale)
        self._ensure_current_drawing_size()

    def _reference_only_scale(self) -> int:
        if self.reference_size_combo.currentData():
            return self.practice_scale
        return 1

    def _drawing_image_for_current_frame(self) -> QImage:
        self._ensure_current_drawing_size()
        return self.drawing_layers[self.current_index]

    def _ensure_current_drawing_size(self) -> None:
        image = self.drawing_layers.get(self.current_index)
        if image is None:
            self.drawing_layers[self.current_index] = self._new_blank_drawing_image(self.current_drawing_size)
            return

        if image.size() == self.current_drawing_size:
            return

        old_size = image.size()
        self.drawing_layers[self.current_index] = self._resized_drawing_image(image)
        history = self.frame_histories.get(self.current_index)
        if history is not None:
            for operation in [*history.undo, *history.redo]:
                self._resize_drawing_operation(operation, old_size, self.current_drawing_size)
            self._history_memory.recalculate(self._histories_by_id())
        frame_id = self.frames[self.current_index].frame_id
        pending = self._pending_stroke_before.get(frame_id)
        if pending is not None:
            kind, before = pending
            self._pending_stroke_before[frame_id] = (kind, self._resized_drawing_image(before))

    @staticmethod
    def _resize_drawing_operation(
        operation: DrawingOperation,
        old_size: QSize,
        new_size: QSize,
    ) -> None:
        if old_size.isEmpty() or new_size.isEmpty():
            return
        scale_x = new_size.width() / old_size.width()
        scale_y = new_size.height() / old_size.height()
        old_rect = operation.rect
        left = round(old_rect.x() * scale_x)
        top = round(old_rect.y() * scale_y)
        right = round((old_rect.x() + old_rect.width()) * scale_x)
        bottom = round((old_rect.y() + old_rect.height()) * scale_y)
        rect = QRect(left, top, max(1, right - left), max(1, bottom - top)).intersected(QRect(QPoint(), new_size))
        if rect.isEmpty():
            return
        operation.rect = rect
        operation.before_patch = operation.before_patch.scaled(
            rect.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        operation.after_patch = operation.after_patch.scaled(
            rect.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
        )

    def _resized_drawing_image(self, image: QImage) -> QImage:
        resized = self._new_blank_drawing_image(self.current_drawing_size)
        if image.isNull():
            return resized

        scaled = image.scaled(
            self.current_drawing_size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter = QPainter(resized)
        painter.drawImage(0, 0, scaled)
        painter.end()
        return resized

    def _new_blank_drawing_image(self, size: QSize) -> QImage:
        image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        return image

    def _load_frame_pixmap(self, frame_id: str) -> QPixmap | None:
        frame = self._frame_for_id(frame_id)
        if frame is None:
            return None
        if frame.pixmap is not None:
            return frame.pixmap
        start = perf_counter()
        if frame.reference_image is not None:
            pixmap = QPixmap.fromImage(frame.reference_image)
        else:
            pixmap = QPixmap(str(frame.path))
        if not pixmap.isNull():
            frame.pixmap = pixmap
        elapsed_ms = (perf_counter() - start) * 1000
        if elapsed_ms >= 16.0:
            print(f"[image-decode] frame_id={frame.frame_id} total={elapsed_ms:.2f}ms")
        return pixmap

    def _preload_adjacent_frames(self) -> None:
        """Warm reference pixmaps after timeline input has been idle briefly."""

        if not self.frame_paths:
            return
        for offset in (-1, 1):
            neighbor = self._onion_neighbor_index(self.current_index, offset)
            if neighbor is not None:
                self._load_frame_pixmap(self.frames[neighbor].frame_id)

    def _playback_interval(self) -> int:
        return max(1, round(1000 / self.playback_fps))

    def set_playback_fps(self, fps: int) -> None:
        self.playback_fps = max(1, min(60, int(fps)))
        self.playback_fps_label.setText(f"{self.playback_fps} FPS")
        self._settings.setValue("playback_fps", self.playback_fps)
        if self.play_timer.isActive():
            self.play_timer.start(self._playback_interval())
        else:
            self.play_timer.setInterval(self._playback_interval())

    def set_export_fps(self, fps: int) -> None:
        fps = max(1, min(60, int(fps)))
        self.export_fps_label.setText(f"{fps} FPS")

    def set_playback_range(self, start: int, end: int) -> None:
        if not self.frames:
            self.playback_range_start = 0
            self.playback_range_end = 0
            return
        last = len(self.frames) - 1
        start = max(0, min(int(start), last))
        end = max(0, min(int(end), last))
        if start > end:
            start, end = end, start
        self.playback_range_start = start
        self.playback_range_end = end
        if hasattr(self, "timeline_ruler"):
            self.timeline_ruler.update()
        for timeline in getattr(self, "group_timeline_widgets", {}).values():
            timeline.viewport().update()
        if self.play_timer.isActive() and not (start <= self.current_index <= end):
            self._playback_exposure_tick = 0
            self.set_current_frame(start, update_timeline=True, ensure_visible=True)

    def show_previous_frame(self) -> None:
        if not self.frame_paths:
            return
        index = (self.current_index - 1) % len(self.frame_paths)
        if self.set_current_frame(index, update_timeline=True):
            self._restart_playback_timer_if_active()

    def show_next_frame(self) -> None:
        if not self.frame_paths:
            return
        index = (self.current_index + 1) % len(self.frame_paths)
        if self.set_current_frame(index, update_timeline=True):
            self._restart_playback_timer_if_active()

    def advance_playback(self) -> None:
        """Advance exactly once with the user-selected fixed FPS."""
        if not self.frame_paths:
            self.play_timer.stop()
            return

        self._playback_exposure_tick += 1
        exposure = self.frame_exposures[self.current_index] if self.current_index < len(self.frame_exposures) else 1
        if self._playback_exposure_tick < exposure:
            self.play_timer.start(self._playback_interval())
            return
        self._playback_exposure_tick = 0
        start = max(0, min(self.playback_range_start, len(self.frame_paths) - 1))
        end = max(start, min(self.playback_range_end, len(self.frame_paths) - 1))
        index = start if self.current_index < start or self.current_index >= end else self.current_index + 1
        if self.set_current_frame(index, update_timeline=True, ensure_visible=True):
            self.play_timer.start(self._playback_interval())
        else:
            self.play_timer.stop()
            self.play_button.setChecked(False)
            self._set_playback_drawing_blocked(False)
            self._refresh_onion_skin_visibility()
            self._update_play_icon()

    def toggle_playback(self, checked: bool) -> None:
        if checked:
            self._playback_exposure_tick = 0
            if self.frames and not (self.playback_range_start <= self.current_index <= self.playback_range_end):
                self.set_current_frame(self.playback_range_start, update_timeline=True, ensure_visible=True)
            self.play_timer.start(self._playback_interval())
        else:
            self.play_timer.stop()
            self._playback_exposure_tick = 0
        self._set_playback_drawing_blocked(checked)
        self._refresh_onion_skin_visibility()
        self._update_play_icon()

    def toggle_playback_from_shortcut(self) -> None:
        self.play_button.setChecked(not self.play_button.isChecked())
        self.toggle_playback(self.play_button.isChecked())

    def pause_playback_for_drawing(self) -> None:
        if self.play_timer.isActive():
            self.play_button.setChecked(False)
            self.toggle_playback(False)

    def _restart_playback_timer_if_active(self) -> None:
        if self.play_timer.isActive():
            self.play_timer.start(self._playback_interval())

    def _set_playback_drawing_blocked(self, blocked: bool) -> None:
        for canvas in self.drawing_canvases:
            canvas.set_drawing_blocked(blocked)

    def _update_play_icon(self) -> None:
        icon_name = QStyle.StandardPixmap.SP_MediaPause if self.play_button.isChecked() else QStyle.StandardPixmap.SP_MediaPlay
        self.play_button.setIcon(self.style().standardIcon(icon_name))

    def _configure_shortcuts(self) -> None:
        control = Qt.KeyboardModifier.ControlModifier
        control_shift = control | Qt.KeyboardModifier.ShiftModifier
        self._shortcut_handlers: dict[tuple[Qt.KeyboardModifier, Qt.Key], Callable[[], None]] = {
            (control, Qt.Key.Key_Z): self.undo_current_frame_drawing,
            (control_shift, Qt.Key.Key_Z): self.redo_current_frame_drawing,
            (control, Qt.Key.Key_Y): self.redo_current_frame_drawing,
            (control, Qt.Key.Key_O): self.import_gif,
            (control, Qt.Key.Key_S): self.save_project,
            (control_shift, Qt.Key.Key_S): self.save_project_as,
            (Qt.KeyboardModifier.NoModifier, Qt.Key.Key_B): lambda: self._select_drawing_tool("brush"),
            (Qt.KeyboardModifier.NoModifier, Qt.Key.Key_E): lambda: self._select_drawing_tool("eraser"),
            (Qt.KeyboardModifier.NoModifier, Qt.Key.Key_1): lambda: self._change_brush_size(-1),
            (Qt.KeyboardModifier.NoModifier, Qt.Key.Key_2): lambda: self._change_brush_size(1),
            (Qt.KeyboardModifier.NoModifier, Qt.Key.Key_A): self.show_previous_frame,
            (Qt.KeyboardModifier.NoModifier, Qt.Key.Key_D): self.show_next_frame,
            (Qt.KeyboardModifier.NoModifier, Qt.Key.Key_Home): lambda: self._jump_to_frame(0),
            (Qt.KeyboardModifier.NoModifier, Qt.Key.Key_End): lambda: self._jump_to_frame(len(self.frame_paths) - 1),
            (Qt.KeyboardModifier.NoModifier, Qt.Key.Key_Delete): self.clear_current_drawing_layer,
            (Qt.KeyboardModifier.NoModifier, Qt.Key.Key_F): self.fit_current_view,
            (Qt.KeyboardModifier.NoModifier, Qt.Key.Key_Space): self.toggle_playback_from_shortcut,
            (Qt.KeyboardModifier.NoModifier, Qt.Key.Key_Tab): self.toggle_settings_panel,
        }

    def _select_drawing_tool(self, tool: str) -> None:
        index = self.tool_combo.findData(tool)
        if index >= 0:
            self.tool_combo.setCurrentIndex(index)

    def _change_brush_size(self, step: int) -> None:
        index = max(0, min(self.brush_size_combo.count() - 1, self.brush_size_combo.currentIndex() + step))
        self.brush_size_combo.setCurrentIndex(index)

    def _jump_to_frame(self, index: int) -> None:
        if not self.frame_paths or index < 0 or index >= len(self.frame_paths):
            return
        if self.set_current_frame(index, update_timeline=True):
            self._restart_playback_timer_if_active()

    def _shortcut_input_is_active(self) -> bool:
        focus_widget = QApplication.focusWidget()
        if isinstance(focus_widget, (QLineEdit, QAbstractSpinBox)):
            return True
        return any(combo.view().isVisible() for combo in self.findChildren(QComboBox))

    def _dispatch_shortcut(self, event: QKeyEvent) -> bool:
        modifiers = event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.ShiftModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        )
        if self._shortcut_input_is_active():
            if not (modifiers & Qt.KeyboardModifier.ControlModifier):
                return False
        handler = self._shortcut_handlers.get((modifiers, event.key()))
        if handler is not None:
            handler()
            return True
        return False

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        if self._dispatch_shortcut(event):
            event.accept()
            return
        super().keyPressEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    window = FrameViewerWindow()
    window.show()
    disable_windows_pointer_feedback(window)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
