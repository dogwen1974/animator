from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QSettings, Qt, QTimer, QSize
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QKeyEvent, QPainter, QPen, QPixmap
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
    QPushButton,
    QSlider,
    QSpinBox,
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


APP_DIR = Path(__file__).resolve().parent
DEFAULT_FRAME_DIR = APP_DIR / "work" / "demo_frames"
MAX_HISTORY_OPERATIONS = 100


@dataclass
class DrawingOperation:
    """One reversible drawing operation for a single animation frame.

    Snapshot storage keeps the current stroke and clear operations simple;
    later operation kinds can add metadata without changing history handling.
    """

    kind: str
    before: QImage
    after: QImage
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class FrameDrawingHistory:
    undo: list[DrawingOperation] = field(default_factory=list)
    redo: list[DrawingOperation] = field(default_factory=list)

    def commit(self, operation: DrawingOperation) -> None:
        self.undo.append(operation)
        if len(self.undo) > MAX_HISTORY_OPERATIONS:
            del self.undo[0]
        self.redo.clear()


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


class FrameViewerWindow(QMainWindow):
    def __init__(self, frame_dir: Path = DEFAULT_FRAME_DIR) -> None:
        super().__init__()
        self.setWindowTitle("GIF Reference Tracing Viewer")
        self.resize(1280, 820)

        self.frame_paths = ensure_demo_png_sequence(frame_dir)
        self.frame_durations = [120 for _ in self.frame_paths]
        self.current_gif_path: Path | None = None
        self.current_index = 0
        self.practice_scale = 3
        self.current_drawing_size = QSize(1, 1)
        self.drawing_layers: dict[int, QImage] = {}
        self.frame_histories: dict[int, FrameDrawingHistory] = {}
        self._pending_stroke_before: dict[int, QImage] = {}
        self._syncing_drawing = False
        self._settings = QSettings("GIF Reference Tracing Viewer", "GIF Reference Tracing Viewer")
        self.thumbnail_display_mode = str(self._settings.value("thumbnail_display_mode", "composite"))
        self._thumbnail_cache: dict[tuple[int, str], QPixmap] = {}
        self._thumbnail_dirty: set[int] = set()
        self._thumbnail_reference_cache: dict[int, QImage] = {}
        self.playback_fps = max(1, min(60, int(self._settings.value("playback_fps", 12))))

        self.play_timer = QTimer(self)
        self.play_timer.setInterval(self._playback_interval())
        self.play_timer.timeout.connect(self.advance_playback)

        self.overlay_canvas = CanvasView()
        self.compare_reference_canvas = CanvasView()
        self.reference_only_canvas = CanvasView()
        self.compare_trace_canvas = BlankCanvasView()
        self.trace_only_canvas = BlankCanvasView()
        self.reference_canvases = [
            self.overlay_canvas,
            self.compare_reference_canvas,
            self.reference_only_canvas,
        ]
        self.all_canvases = [
            self.overlay_canvas,
            self.compare_reference_canvas,
            self.reference_only_canvas,
            self.compare_trace_canvas,
            self.trace_only_canvas,
        ]
        self.drawing_canvases = [
            self.overlay_canvas,
            self.compare_trace_canvas,
            self.trace_only_canvas,
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

        self.import_step_spinbox = QSpinBox()
        self.import_step_spinbox.setRange(1, 999)
        self.import_step_spinbox.setValue(1)
        self.import_step_spinbox.setEnabled(False)
        self.import_step_spinbox.setFixedWidth(72)
        self.import_step_spinbox.setToolTip("导入 GIF 后调整抽帧步数")
        self.import_step_spinbox.valueChanged.connect(self.reload_current_gif_with_step)

        self.previous_button = QPushButton("上一帧")
        self.previous_button.clicked.connect(self.show_previous_frame)

        self.play_button = QToolButton()
        self.play_button.setCheckable(True)
        self.play_button.setToolTip("Play / pause")
        self.play_button.clicked.connect(self.toggle_playback)

        self.playback_fps_spinbox = QSpinBox()
        self.playback_fps_spinbox.setRange(1, 60)
        self.playback_fps_spinbox.setValue(self.playback_fps)
        self.playback_fps_spinbox.setSuffix(" FPS")
        self.playback_fps_spinbox.setToolTip("播放速度")
        self.playback_fps_spinbox.valueChanged.connect(self.set_playback_fps)

        self.next_button = QPushButton("下一帧")
        self.next_button.clicked.connect(self.show_next_frame)

        self.fit_button = QPushButton("适应窗口")
        self.fit_button.clicked.connect(self.fit_current_view)

        self.tool_combo = QComboBox()
        self.tool_combo.addItem("画笔", "brush")
        self.tool_combo.addItem("软边画笔", "soft_brush")
        self.tool_combo.addItem("方形画笔", "square_brush")
        self.tool_combo.addItem("橡皮", "eraser")
        self.tool_combo.currentIndexChanged.connect(self.set_drawing_tool)

        self.square_brush_angle_combo = QComboBox()
        self.square_brush_angle_combo.addItem("固定 45°", False)
        self.square_brush_angle_combo.addItem("沿轨迹旋转", True)
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

        self.brush_hardness_label = QLabel("45%")
        self.brush_hardness_slider = QSlider(Qt.Orientation.Horizontal)
        self.brush_hardness_slider.setRange(0, 100)
        self.brush_hardness_slider.setValue(45)
        self.brush_hardness_slider.setToolTip("软边画笔硬度")
        self.brush_hardness_slider.valueChanged.connect(self.set_brush_hardness)

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
        self.view_mode_combo.addItems(["叠加模式", "左右对比模式", "只看参考", "只看临摹"])
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
        toolbar.addWidget(QLabel(" Step "))
        toolbar.addWidget(self.import_step_spinbox)
        toolbar.addSeparator()
        toolbar.addWidget(self.previous_button)
        toolbar.addWidget(self.play_button)
        toolbar.addWidget(self.playback_fps_spinbox)
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
        drawing_layout.addRow("方头方向", self.square_brush_angle_combo)
        drawing_layout.addRow("颜色", self.brush_color_combo)
        drawing_layout.addRow("粗细", self.brush_size_combo)
        brush_opacity_row = QHBoxLayout()
        brush_opacity_row.addWidget(self.brush_opacity_slider, 1)
        brush_opacity_row.addWidget(self.brush_opacity_label)
        drawing_layout.addRow("透明度", brush_opacity_row)
        brush_hardness_row = QHBoxLayout()
        brush_hardness_row.addWidget(self.brush_hardness_slider, 1)
        brush_hardness_row.addWidget(self.brush_hardness_label)
        drawing_layout.addRow("软边硬度", brush_hardness_row)
        drawing_layout.addRow(self.clear_drawing_button)
        layout.addWidget(drawing_group)

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
        self.import_step_spinbox.blockSignals(True)
        self.import_step_spinbox.setValue(1)
        self.import_step_spinbox.setEnabled(True)
        self.import_step_spinbox.blockSignals(False)
        self.load_current_gif(step=1)

    def reload_current_gif_with_step(self) -> None:
        if self.current_gif_path is None:
            return
        self.load_current_gif(step=self.import_step_spinbox.value())

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
        self.drawing_layers.clear()
        self.frame_histories.clear()
        self._pending_stroke_before.clear()

        self._load_timeline()
        self.timeline.setCurrentRow(0)

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

    def set_brush_size(self, *_args) -> None:
        size = self.brush_size_combo.currentData()
        for canvas in self.drawing_canvases:
            canvas.set_brush_size(size)

    def begin_drawing_operation(self, image: QImage) -> None:
        """Capture the canvas once when a new pen or eraser stroke begins."""
        if self._syncing_drawing:
            return
        self._pending_stroke_before[self.current_index] = image.copy()

    def update_current_drawing_layer(self, image: QImage) -> None:
        if self._syncing_drawing:
            return
        before = self._pending_stroke_before.pop(self.current_index, None)
        after = image.copy()
        self.drawing_layers[self.current_index] = after
        if before is not None:
            self._commit_drawing_operation(
                DrawingOperation("stroke", before=before, after=after.copy())
            )
        self.sync_drawing_layer_to_views()
        self._mark_frame_thumbnail_dirty(self.current_index)

    def clear_current_drawing_layer(self) -> None:
        current_image = self._drawing_image_for_current_frame().copy()
        blank = self._new_blank_drawing_image(self.current_drawing_size)
        self.drawing_layers[self.current_index] = blank
        self._pending_stroke_before.pop(self.current_index, None)
        self._commit_drawing_operation(
            DrawingOperation("clear", before=current_image, after=blank.copy())
        )
        self.sync_drawing_layer_to_views()
        self._mark_frame_thumbnail_dirty(self.current_index)

    def undo_current_frame_drawing(self) -> None:
        history = self.frame_histories.get(self.current_index)
        if history is None or not history.undo:
            return
        operation = history.undo.pop()
        self.drawing_layers[self.current_index] = operation.before.copy()
        history.redo.append(operation)
        self._pending_stroke_before.pop(self.current_index, None)
        self.sync_drawing_layer_to_views()
        self._mark_frame_thumbnail_dirty(self.current_index)

    def redo_current_frame_drawing(self) -> None:
        history = self.frame_histories.get(self.current_index)
        if history is None or not history.redo:
            return
        operation = history.redo.pop()
        self.drawing_layers[self.current_index] = operation.after.copy()
        history.undo.append(operation)
        self._pending_stroke_before.pop(self.current_index, None)
        self.sync_drawing_layer_to_views()
        self._mark_frame_thumbnail_dirty(self.current_index)

    def _commit_drawing_operation(self, operation: DrawingOperation) -> None:
        history = self.frame_histories.setdefault(self.current_index, FrameDrawingHistory())
        history.commit(operation)

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

        self.drawing_layers[self.current_index] = self._resized_drawing_image(image)
        history = self.frame_histories.get(self.current_index)
        if history is not None:
            for operation in [*history.undo, *history.redo]:
                operation.before = self._resized_drawing_image(operation.before)
                operation.after = self._resized_drawing_image(operation.after)
        pending = self._pending_stroke_before.get(self.current_index)
        if pending is not None:
            self._pending_stroke_before[self.current_index] = self._resized_drawing_image(pending)

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
        return QPixmap(str(self.frame_paths[index]))

    def _playback_interval(self) -> int:
        return max(1, round(1000 / self.playback_fps))

    def set_playback_fps(self, fps: int) -> None:
        self.playback_fps = max(1, min(60, int(fps)))
        self._settings.setValue("playback_fps", self.playback_fps)
        if self.play_timer.isActive():
            self.play_timer.start(self._playback_interval())
        else:
            self.play_timer.setInterval(self._playback_interval())

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

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        if self._shortcut_input_is_active():
            super().keyPressEvent(event)
            return

        modifiers = event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.ShiftModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        )
        handler = self._shortcut_handlers.get((modifiers, event.key()))
        if handler is not None:
            handler()
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
