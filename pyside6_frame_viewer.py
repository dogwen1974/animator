from __future__ import annotations

import ctypes
import io
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QPoint, QRect, QSettings, Qt, QTimer, QSize
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QKeyEvent, QMouseEvent, QPainter, QPen, QPixmap
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
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QMainWindow,
    QMenu,
    QPushButton,
    QSlider,
    QSplitter,
    QStackedWidget,
    QStyle,
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


class FrameViewerWindow(QMainWindow):
    def __init__(self, frame_dir: Path = DEFAULT_FRAME_DIR) -> None:
        super().__init__()
        self.setWindowTitle("GIF Reference Tracing Viewer")
        self.resize(1280, 820)

        self.frame_paths = ensure_demo_png_sequence(frame_dir)
        self.frame_durations = [120 for _ in self.frame_paths]
        self.current_gif_path: Path | None = None
        self.current_project_path: Path | None = None
        self.current_index = 0
        self.practice_scale = 3
        self.current_drawing_size = QSize(1, 1)
        self.drawing_layers: dict[int, QImage] = {}
        self.frame_histories: dict[int, FrameDrawingHistory] = {}
        self._history_memory = HistoryMemoryManager()
        self._pending_stroke_before: dict[int, tuple[str, QImage]] = {}
        self._syncing_drawing = False
        self._settings = QSettings("GIF Reference Tracing Viewer", "GIF Reference Tracing Viewer")
        self.thumbnail_display_mode = str(self._settings.value("thumbnail_display_mode", "composite"))
        self._thumbnail_cache: dict[tuple[int, str], QPixmap] = {}
        self._thumbnail_dirty: set[int] = set()
        self._thumbnail_reference_cache: dict[int, QImage] = {}
        self._project_reference_images: dict[int, QImage] = {}
        self.playback_fps = max(1, min(60, int(self._settings.value("playback_fps", 12))))

        self.play_timer = QTimer(self)
        self.play_timer.setInterval(self._playback_interval())
        self.play_timer.timeout.connect(self.advance_playback)

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

        self.timeline = QListWidget()
        self.timeline.setViewMode(QListView.ViewMode.IconMode)
        self.timeline.setFlow(QListView.Flow.LeftToRight)
        self.timeline.setWrapping(False)
        self.timeline.setMovement(QListView.Movement.Static)
        self.timeline.setResizeMode(QListView.ResizeMode.Adjust)
        self.timeline.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.timeline.setIconSize(QSize(96, 54))
        self.timeline.setGridSize(QSize(108, 82))
        self.timeline.setSpacing(4)
        self.timeline.setFixedHeight(104)
        self.timeline.setStyleSheet(
            "QListWidget::item { border: 2px solid transparent; padding: 3px; }"
            "QListWidget::item:selected {"
            " background: #d1d5db; border-color: #6b7280; color: #111827; }"
        )
        self.timeline.currentRowChanged.connect(self.set_current_frame)
        self.timeline.horizontalScrollBar().valueChanged.connect(self._schedule_visible_thumbnail_refresh)
        self.timeline.viewport().installEventFilter(self)
        self._thumbnail_refresh_timer = QTimer(self)
        self._thumbnail_refresh_timer.setSingleShot(True)
        self._thumbnail_refresh_timer.timeout.connect(self._refresh_visible_thumbnails)

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
        self.view_mode_combo.currentIndexChanged.connect(self.set_view_mode)

        self.settings_button = QPushButton("画布设置")
        self.settings_button.clicked.connect(self.toggle_settings_panel)

        self.frame_label = QLabel()
        self.frame_label.setMinimumWidth(150)

        self.onion_skin_checkbox = QCheckBox("Onion Skin")
        self.onion_skin_checkbox.toggled.connect(self.set_onion_skin_enabled)

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
        central_layout.addWidget(self.view_stack, 1)
        central_layout.addWidget(self._create_timeline_panel())
        self.setCentralWidget(central)

        self.settings_dock = QDockWidget("画布设置", self)
        self.settings_dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        self.settings_dock.setWidget(self._create_settings_panel())
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.settings_dock)
        self.settings_dock.hide()

    def _create_timeline_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("缩略图显示"))
        controls.addWidget(self.thumbnail_display_combo)
        controls.addStretch(1)
        layout.addLayout(controls)
        layout.addWidget(self.timeline)
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
        toolbar.addWidget(self.frame_label)
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

    def _load_timeline(self) -> None:
        self.timeline.clear()
        self._thumbnail_cache.clear()
        self._thumbnail_dirty = set(range(len(self.frame_paths)))
        self._thumbnail_reference_cache.clear()
        for index, path in enumerate(self.frame_paths):
            item = QListWidgetItem(f"{index:04d}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.timeline.addItem(item)
        self._schedule_visible_thumbnail_refresh()

    def set_thumbnail_display_mode(self, *_args) -> None:
        self.thumbnail_display_mode = self.thumbnail_display_combo.currentData()
        self._settings.setValue("thumbnail_display_mode", self.thumbnail_display_mode)
        self._schedule_visible_thumbnail_refresh()

    def generate_frame_thumbnail(self, frame_index: int, display_mode: str | None = None) -> QPixmap:
        """Create one centered thumbnail without canvas-only helper overlays."""
        mode = display_mode or self.thumbnail_display_mode
        cache_key = (frame_index, mode)
        cached = self._thumbnail_cache.get(cache_key)
        if cached is not None and frame_index not in self._thumbnail_dirty:
            return cached

        reference = self._reference_image_for_thumbnail(frame_index)
        if reference.isNull():
            return QPixmap()

        if mode == "reference":
            content = reference
        else:
            canvas_size = QSize(
                reference.width() * self.practice_scale,
                reference.height() * self.practice_scale,
            )
            content = QImage(canvas_size, QImage.Format.Format_ARGB32_Premultiplied)
            content.fill(Qt.GlobalColor.white if mode == "drawing" else Qt.GlobalColor.transparent)

            painter = QPainter(content)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            if mode == "composite":
                painter.drawImage(content.rect(), reference)
            drawing_layer = self._drawing_layer_for_thumbnail(frame_index, canvas_size)
            if not drawing_layer.isNull():
                painter.drawImage(0, 0, drawing_layer)
            painter.end()

        thumbnail = self._centered_thumbnail_pixmap(content)
        self._thumbnail_cache[cache_key] = thumbnail
        self._thumbnail_dirty.discard(frame_index)
        return thumbnail

    def _reference_image_for_thumbnail(self, frame_index: int) -> QImage:
        cached = self._thumbnail_reference_cache.get(frame_index)
        if cached is not None:
            return cached
        if frame_index < 0 or frame_index >= len(self.frame_paths):
            return QImage()
        project_image = self._project_reference_images.get(frame_index)
        if project_image is not None:
            image = project_image.copy()
        else:
            image = QImage(str(self.frame_paths[frame_index])).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        self._thumbnail_reference_cache[frame_index] = image
        return image

    def _drawing_layer_for_thumbnail(self, frame_index: int, canvas_size: QSize) -> QImage:
        drawing_layer = self.drawing_layers.get(frame_index)
        if drawing_layer is None or drawing_layer.isNull():
            return QImage()
        if drawing_layer.size() == canvas_size:
            return drawing_layer
        return drawing_layer.scaled(
            canvas_size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _centered_thumbnail_pixmap(self, image: QImage) -> QPixmap:
        icon_size = self.timeline.iconSize()
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

    def _mark_frame_thumbnail_dirty(self, frame_index: int) -> None:
        self._thumbnail_dirty.add(frame_index)
        for mode in ("reference", "drawing", "composite"):
            self._thumbnail_cache.pop((frame_index, mode), None)
        self._refresh_frame_thumbnail_if_visible(frame_index)
        self._schedule_visible_thumbnail_refresh()

    def _mark_all_thumbnails_dirty(self) -> None:
        self._thumbnail_dirty.update(range(len(self.frame_paths)))
        self._thumbnail_cache.clear()
        self._schedule_visible_thumbnail_refresh()

    def _schedule_visible_thumbnail_refresh(self, *_args) -> None:
        if not self._thumbnail_refresh_timer.isActive():
            self._thumbnail_refresh_timer.start(0)

    def _refresh_visible_thumbnails(self) -> None:
        visible_rect = self.timeline.viewport().rect()
        for index in range(self.timeline.count()):
            item = self.timeline.item(index)
            if self.timeline.visualItemRect(item).intersects(visible_rect):
                self._update_timeline_item_thumbnail(index)

    def _refresh_frame_thumbnail_if_visible(self, frame_index: int) -> None:
        if frame_index < 0 or frame_index >= self.timeline.count():
            return
        item = self.timeline.item(frame_index)
        if self.timeline.visualItemRect(item).intersects(self.timeline.viewport().rect()):
            self._update_timeline_item_thumbnail(frame_index)

    def _update_timeline_item_thumbnail(self, frame_index: int) -> None:
        if frame_index < 0 or frame_index >= self.timeline.count():
            return
        self.timeline.item(frame_index).setIcon(
            QIcon(self.generate_frame_thumbnail(frame_index, self.thumbnail_display_mode))
        )

    def eventFilter(self, watched, event):  # type: ignore[override]
        if watched is self.timeline.viewport() and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
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
            self.frame_paths = export_png_sequence(frames, output_dir)
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", f"导出 PNG 序列失败：\n{exc}")
            return

        self.frame_durations = [frame.duration if frame.duration > 0 else 120 for frame in frames]
        self.current_index = 0
        self.current_project_path = None
        self._project_reference_images.clear()
        self.drawing_layers.clear()
        self.frame_histories.clear()
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
            project_path.parent.mkdir(parents=True, exist_ok=True)
            manifest: dict[str, object] = {
                "version": 1,
                "frame_durations": self.frame_durations,
                "practice_scale": self.practice_scale,
                "current_index": self.current_index,
                "reference_frames": [],
                "drawing_layers": {},
            }
            with zipfile.ZipFile(project_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                reference_entries: list[str] = []
                drawing_entries: dict[str, str] = {}
                for index, path in enumerate(self.frame_paths):
                    reference = self._reference_image_for_thumbnail(index)
                    if reference.isNull():
                        raise ValueError(f"无法读取参考帧：{path}")
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
                if manifest.get("version") != 1:
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

                raw_durations = manifest.get("frame_durations", [])
                durations = [max(1, int(value)) for value in raw_durations] if isinstance(raw_durations, list) else []
                if len(durations) != len(restored_paths):
                    durations = [120 for _ in restored_paths]
                saved_scale = int(manifest.get("practice_scale", 3))
                saved_scale = saved_scale if saved_scale in (1, 2, 3, 4) else 3
                saved_index = max(0, min(int(manifest.get("current_index", 0)), len(restored_paths) - 1))
        except Exception as exc:
            QMessageBox.critical(self, "打开工程失败", str(exc))
            return

        self.play_timer.stop()
        self.play_button.setChecked(False)
        self._set_playback_drawing_blocked(False)
        self.current_gif_path = None
        self.current_project_path = project_path
        self.import_step_slider.setEnabled(False)
        self.frame_paths = restored_paths
        self._project_reference_images = restored_references
        self.frame_durations = durations
        self.drawing_layers = drawing_layers
        self.frame_histories.clear()
        self._history_memory.reset()
        self._pending_stroke_before.clear()
        self.current_index = saved_index
        self.practice_scale_combo.blockSignals(True)
        self.practice_scale_combo.setCurrentIndex(self.practice_scale_combo.findData(saved_scale))
        self.practice_scale_combo.blockSignals(False)
        self.practice_scale = saved_scale
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
                pil_frames.append(Image.open(io.BytesIO(self._qimage_to_png_bytes(image))).convert("RGB"))
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
        reference = self._reference_image_for_thumbnail(frame_index)
        if reference.isNull():
            raise ValueError(f"无法读取第 {frame_index + 1} 帧参考图。")
        canvas_size = QSize(reference.width() * self.practice_scale, reference.height() * self.practice_scale)
        canvas = QImage(canvas_size, QImage.Format.Format_RGB32)
        canvas.fill(Qt.GlobalColor.white)
        painter = QPainter(canvas)
        drawing = self._drawing_layer_for_thumbnail(frame_index, canvas_size)
        if not drawing.isNull():
            painter.drawImage(0, 0, drawing)
        painter.end()
        return canvas

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

        self.current_index = index
        previous_pixmap = self._load_frame_pixmap(index - 1)
        current_pixmap = self._load_frame_pixmap(index)
        next_pixmap = self._load_frame_pixmap(index + 1)

        if current_pixmap is None or current_pixmap.isNull():
            return False

        self._update_practice_canvas_size(current_pixmap)

        self.overlay_canvas.set_content_scale(self.practice_scale)
        self.compare_reference_canvas.set_content_scale(self.practice_scale)
        self.reference_only_canvas.set_content_scale(self._reference_only_scale())
        self.reference_float_canvas.set_content_scale(self.practice_scale)

        for canvas in self.reference_canvases:
            canvas.set_frame_layers(previous_pixmap, current_pixmap, next_pixmap)

        self.sync_drawing_layer_to_views()
        self._refresh_onion_skin_visibility()

        if update_timeline and self.timeline.currentRow() != index:
            self.timeline.blockSignals(True)
            try:
                self.timeline.setCurrentRow(index)
            finally:
                self.timeline.blockSignals(False)

        if ensure_visible and 0 <= index < self.timeline.count():
            self.timeline.scrollToItem(
                self.timeline.item(index),
                QAbstractItemView.ScrollHint.EnsureVisible,
            )

        self.frame_label.setText(f"帧 {index + 1} / {len(self.frame_paths)}")
        self._restart_playback_timer_if_active()
        return True

    def set_view_mode(self, index: int) -> None:
        self.view_stack.setCurrentIndex(index)
        if self.view_stack.currentWidget() is self.float_reference_view:
            self.reference_float_window.show()
            self.reference_float_window.raise_()
            self.reference_float_window.canvas.fit_to_view()

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
        self._pending_stroke_before[self.current_index] = (kind, image.copy())

    def update_current_drawing_layer(self, image: QImage) -> None:
        if self._syncing_drawing:
            return
        pending = self._pending_stroke_before.pop(self.current_index, None)
        after = image.copy()
        self.drawing_layers[self.current_index] = after
        if pending is not None:
            kind, before = pending
            self._commit_drawing_operation_from_images(kind, before, after)
        self.sync_drawing_layer_to_views()
        self._mark_frame_thumbnail_dirty(self.current_index)

    def clear_current_drawing_layer(self) -> None:
        current_image = self._drawing_image_for_current_frame().copy()
        blank = self._new_blank_drawing_image(self.current_drawing_size)
        self.drawing_layers[self.current_index] = blank
        self._pending_stroke_before.pop(self.current_index, None)
        self._commit_drawing_operation_from_images("clear", current_image, blank)
        self.sync_drawing_layer_to_views()
        self._mark_frame_thumbnail_dirty(self.current_index)

    def undo_current_frame_drawing(self) -> None:
        history = self.frame_histories.get(self.current_index)
        if history is None or not history.undo:
            return
        operation = history.undo.pop()
        self._apply_drawing_operation_patch(operation, operation.before_patch)
        history.redo.append(operation)
        self._pending_stroke_before.pop(self.current_index, None)
        self.sync_drawing_layer_to_views()
        self._mark_frame_thumbnail_dirty(self.current_index)

    def redo_current_frame_drawing(self) -> None:
        history = self.frame_histories.get(self.current_index)
        if history is None or not history.redo:
            return
        operation = history.redo.pop()
        self._apply_drawing_operation_patch(operation, operation.after_patch)
        history.undo.append(operation)
        self._pending_stroke_before.pop(self.current_index, None)
        self.sync_drawing_layer_to_views()
        self._mark_frame_thumbnail_dirty(self.current_index)

    def _commit_drawing_operation(self, operation: DrawingOperation) -> None:
        history = self.frame_histories.setdefault(self.current_index, FrameDrawingHistory())
        self._history_memory.commit(
            self.current_index,
            history,
            operation,
            self.frame_histories,
        )

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
        target_rect = operation.rect.intersected(image.rect())
        if target_rect.isEmpty():
            return
        source_rect = QRect(
            target_rect.x() - operation.rect.x(),
            target_rect.y() - operation.rect.y(),
            target_rect.width(),
            target_rect.height(),
        )
        painter = QPainter(image)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.drawImage(target_rect.topLeft(), patch.copy(source_rect))
        painter.end()
        self.drawing_layers[self.current_index] = image

    def sync_drawing_layer_to_views(self) -> None:
        image = self._drawing_image_for_current_frame()
        self._syncing_drawing = True
        try:
            for canvas in self.drawing_canvases:
                canvas.set_drawing_image(image)
        finally:
            self._syncing_drawing = False
        self._refresh_drawing_onion_layers()

    def export_current_drawing_png(self, path: str | Path) -> bool:
        return self._drawing_image_for_current_frame().save(str(path), "PNG")

    def set_onion_skin_enabled(self, enabled: bool) -> None:
        self._refresh_onion_skin_visibility()

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

        previous = self._drawing_onion_image_for_frame(self.current_index - 1)
        next_image = self._drawing_onion_image_for_frame(self.current_index + 1)
        for canvas in self.drawing_canvases:
            canvas.set_drawing_onion_layers(previous, next_image)
        self._refresh_onion_skin_visibility()

    def _drawing_onion_image_for_frame(self, frame_index: int) -> QImage | None:
        if frame_index < 0 or frame_index >= len(self.frame_paths):
            return None
        image = self.drawing_layers.get(frame_index)
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
        self.compare_trace_canvas.set_canvas_size(width, height, self.practice_scale)
        self.trace_only_canvas.set_canvas_size(width, height, self.practice_scale)
        self.float_trace_canvas.set_canvas_size(width, height, self.practice_scale)
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
            self._history_memory.recalculate(self.frame_histories)
        pending = self._pending_stroke_before.get(self.current_index)
        if pending is not None:
            kind, before = pending
            self._pending_stroke_before[self.current_index] = (kind, self._resized_drawing_image(before))

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

    def _load_frame_pixmap(self, index: int) -> QPixmap | None:
        if index < 0 or index >= len(self.frame_paths):
            return None
        project_image = self._project_reference_images.get(index)
        if project_image is not None:
            return QPixmap.fromImage(project_image)
        return QPixmap(str(self.frame_paths[index]))

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

        index = (self.current_index + 1) % len(self.frame_paths)
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
            self.play_timer.start(self._playback_interval())
        else:
            self.play_timer.stop()
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
        if self._shortcut_input_is_active() and not (modifiers & Qt.KeyboardModifier.ControlModifier):
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
