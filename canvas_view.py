from __future__ import annotations

import math
from time import perf_counter

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QLinearGradient, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene, QGraphicsView, QWidget


def _draw_scene_grid(
    painter: QPainter,
    exposed_rect: QRectF,
    bounds_rect: QRectF,
    grid_size: int,
    grid_color: QColor,
    grid_opacity: float,
) -> None:
    grid_rect = exposed_rect.intersected(bounds_rect)
    if grid_rect.isEmpty():
        return

    minor_color = QColor(grid_color)
    major_color = QColor(grid_color)
    minor_color.setAlpha(max(0, min(255, int(255 * grid_opacity))))
    major_color.setAlpha(max(0, min(255, int(255 * min(1.0, grid_opacity * 1.45)))))
    minor_pen = QPen(minor_color, 0)
    major_pen = QPen(major_color, 0)

    left = math.floor(grid_rect.left() / grid_size) * grid_size
    right = math.ceil(grid_rect.right() / grid_size) * grid_size
    top = math.floor(grid_rect.top() / grid_size) * grid_size
    bottom = math.ceil(grid_rect.bottom() / grid_size) * grid_size

    painter.save()
    painter.setClipRect(bounds_rect)

    x = left
    while x <= right:
        painter.setPen(major_pen if int(round(x / grid_size)) % 5 == 0 else minor_pen)
        painter.drawLine(int(x), int(top), int(x), int(bottom))
        x += grid_size

    y = top
    while y <= bottom:
        painter.setPen(major_pen if int(round(y / grid_size)) % 5 == 0 else minor_pen)
        painter.drawLine(int(left), int(y), int(right), int(y))
        y += grid_size

    painter.restore()


class SoftBrushRenderer:
    """Rasterize one soft brush stroke into a maximum-coverage alpha mask.

    Each input segment is a variable-radius capsule.  Unlike a sequence of
    semi-transparent dabs, softRound keeps the largest coverage at each pixel,
    so event density and overlapping samples cannot darken one stroke.
    """

    def __init__(self) -> None:
        self._mask = QImage()
        self._target_size = QSize()
        self._dirty_rect = QRect()
        self._size = 1.0
        self._hardness = 0.45
        self._edge_gamma = 1.5
        self._flow = 0.25
        self._mode = "softRound"
        self._last_render_key: tuple[int, bool] | None = None
        self.raw_points: list[QPointF] = []
        self.resampled_points: list[QPointF] = []
        self.capsules: list[tuple[QPointF, QPointF, float, float]] = []

    @property
    def mask(self) -> QImage:
        return self._mask

    def begin(
        self,
        size: float,
        hardness: int,
        edge_gamma: float,
        flow: float,
        mode: str,
        start_point: QPointF,
    ) -> None:
        self._size = max(1.0, float(size))
        self._hardness = max(0.0, min(1.0, hardness / 100.0))
        self._edge_gamma = max(0.1, float(edge_gamma))
        self._flow = max(0.01, min(1.0, float(flow)))
        self._mode = "airbrush" if mode == "airbrush" else "softRound"
        self._mask = QImage()
        self._dirty_rect = QRect()
        self._last_render_key = None
        self.raw_points = [QPointF(start_point)]
        self.resampled_points = []
        self.capsules = []

    def reset(self) -> None:
        self._mask = QImage()
        self._dirty_rect = QRect()
        self._last_render_key = None
        self.raw_points = []
        self.resampled_points = []
        self.capsules = []

    def add_raw_point(self, point: QPointF) -> None:
        if not self.raw_points or not self._same_point(self.raw_points[-1], point):
            self.raw_points.append(QPointF(point))

    def add_capsule(
        self,
        start: QPointF,
        end: QPointF,
        start_radius: float | None = None,
        end_radius: float | None = None,
        start_strength: float = 1.0,
        end_strength: float = 1.0,
    ) -> None:
        start_radius = max(0.5, start_radius if start_radius is not None else self._size / 2)
        end_radius = max(0.5, end_radius if end_radius is not None else self._size / 2)
        self._ensure_mask()
        self._rasterize_capsule(start, end, start_radius, end_radius, start_strength, end_strength)
        self.capsules.append((QPointF(start), QPointF(end), start_radius, end_radius))
        if not self.resampled_points:
            self.resampled_points.append(QPointF(start))
        if not self._same_point(self.resampled_points[-1], end):
            self.resampled_points.append(QPointF(end))

    def add_dot(self, point: QPointF, radius: float | None = None, strength: float = 1.0) -> None:
        self.add_capsule(point, point, radius, radius, strength, strength)

    def render_colored(self, target: QImage, color: QColor, *, mask_only: bool = False) -> None:
        if self._mask.isNull():
            return
        render_key = (color.rgba(), mask_only)
        if render_key != self._last_render_key:
            self._dirty_rect = target.rect()
            self._last_render_key = render_key
        rect = self._dirty_rect.intersected(target.rect())
        if rect.isEmpty():
            return
        painter = QPainter(target)
        painter.setClipRect(rect)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(rect, Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.fillRect(rect, Qt.GlobalColor.white if mask_only else color)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawImage(0, 0, self._mask)
        painter.end()
        self._dirty_rect = QRect()

    def draw_debug(self, painter: QPainter, mode: str) -> None:
        if mode == "raw" and self.raw_points:
            painter.save()
            painter.setPen(QPen(QColor("#ef4444"), 0))
            painter.setBrush(QColor("#ef4444"))
            for point in self.raw_points:
                painter.drawEllipse(point, 1.8, 1.8)
            painter.restore()
        elif mode == "resampled" and len(self.resampled_points) > 1:
            painter.save()
            painter.setPen(QPen(QColor("#2563eb"), 0))
            for start, end in zip(self.resampled_points, self.resampled_points[1:]):
                painter.drawLine(start, end)
            painter.restore()
        elif mode == "capsules":
            painter.save()
            painter.setPen(QPen(QColor(22, 163, 74, 210), 0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for start, end, start_radius, end_radius in self.capsules:
                painter.drawLine(start, end)
                painter.drawEllipse(start, start_radius, start_radius)
                painter.drawEllipse(end, end_radius, end_radius)
            painter.restore()

    def _ensure_mask(self) -> None:
        if not self._mask.isNull():
            return
        # The drawing layer is the logical canvas's real pixel buffer.  Keeping
        # the mask at this size avoids low-resolution blur and DPR resampling.
        self._mask = QImage(self._canvas_size, QImage.Format.Format_Alpha8)
        self._mask.fill(0)

    @property
    def _canvas_size(self) -> QSize:
        return self._target_size

    def set_canvas_size(self, size: QSize) -> None:
        self._target_size = QSize(size)

    def _rasterize_capsule(
        self,
        start: QPointF,
        end: QPointF,
        start_radius: float,
        end_radius: float,
        start_strength: float,
        end_strength: float,
    ) -> None:
        if self._mask.isNull():
            return
        radius = max(start_radius, end_radius)
        left = max(0, math.floor(min(start.x(), end.x()) - radius - 1))
        right = min(self._mask.width() - 1, math.ceil(max(start.x(), end.x()) + radius + 1))
        top = max(0, math.floor(min(start.y(), end.y()) - radius - 1))
        bottom = min(self._mask.height() - 1, math.ceil(max(start.y(), end.y()) + radius + 1))
        if left > right or top > bottom:
            return

        dx = end.x() - start.x()
        dy = end.y() - start.y()
        length_squared = dx * dx + dy * dy
        for y in range(top, bottom + 1):
            row = self._mask.scanLine(y)
            py = y + 0.5
            for x in range(left, right + 1):
                px = x + 0.5
                if length_squared <= 1e-9:
                    t = 0.0
                    center_x, center_y = start.x(), start.y()
                else:
                    t = max(0.0, min(1.0, ((px - start.x()) * dx + (py - start.y()) * dy) / length_squared))
                    center_x = start.x() + dx * t
                    center_y = start.y() + dy * t
                current_radius = start_radius + (end_radius - start_radius) * t
                strength = start_strength + (end_strength - start_strength) * t
                distance = math.hypot(px - center_x, py - center_y)
                coverage = self._coverage(distance, current_radius) * max(0.0, min(1.0, strength))
                if coverage <= 0.0:
                    continue
                previous = row[x]
                if self._mode == "airbrush":
                    incoming = round(255 * coverage * self._flow)
                    alpha = 255 - ((255 - previous) * (255 - incoming) // 255)
                else:
                    alpha = max(previous, round(255 * coverage))
                if alpha > previous:
                    row[x] = alpha

        rect = QRect(left, top, right - left + 1, bottom - top + 1)
        self._dirty_rect = rect if self._dirty_rect.isNull() else self._dirty_rect.united(rect)

    def _coverage(self, distance: float, radius: float) -> float:
        if distance >= radius:
            return 0.0
        # Bias low hardness toward a very small core, leaving most of the radius
        # for a continuous feather instead of a semi-transparent hard edge.
        inner_radius = radius * (self._hardness ** 1.35)
        if distance <= inner_radius or radius - inner_radius <= 1e-6:
            return 1.0
        progress = (distance - inner_radius) / (radius - inner_radius)
        smoothstep = progress * progress * (3.0 - 2.0 * progress)
        return max(0.0, min(1.0, (1.0 - smoothstep) ** self._edge_gamma))

    @staticmethod
    def _same_point(first: QPointF, second: QPointF) -> bool:
        return abs(first.x() - second.x()) < 0.01 and abs(first.y() - second.y()) < 0.01


class PencilRenderer:
    """Textured graphite deposition for one pencil stroke.

    The renderer owns a grayscale alpha mask rather than borrowing the hard
    brush path.  Cached material tips carry deterministic multi-scale grain;
    each stamped placement varies continuously with travelled distance.
    """

    def __init__(self) -> None:
        self._mask = QImage()
        self._target_size = QSize()
        self._dirty_rect = QRect()
        self._last_render_key: tuple[int, bool] | None = None
        self._tip_cache: dict[tuple[int, int, int, int], QImage] = {}
        self._size = 4.0
        self._grain = 55
        self._density = 0.68
        self._pressure_size = True
        self._pressure_density = True
        self._tip_variation = 0.2
        self._distance = 0.0
        self._smoothed_point: QPointF | None = None

    def set_canvas_size(self, size: QSize) -> None:
        self._target_size = QSize(size)

    def begin(
        self,
        size: float,
        grain: int,
        density: float,
        pressure_size: bool,
        pressure_density: bool,
        tip_variation: float,
    ) -> None:
        self._size = max(1.0, float(size))
        self._grain = max(0, min(100, int(grain)))
        self._density = max(0.05, min(1.0, float(density)))
        self._pressure_size = pressure_size
        self._pressure_density = pressure_density
        self._tip_variation = max(0.0, min(1.0, float(tip_variation)))
        self._mask = QImage()
        self._dirty_rect = QRect()
        self._last_render_key = None
        self._distance = 0.0
        self._smoothed_point = None

    def reset(self) -> None:
        self._mask = QImage()
        self._dirty_rect = QRect()
        self._last_render_key = None
        self._distance = 0.0
        self._smoothed_point = None

    def clear_tip_cache(self) -> None:
        self._tip_cache.clear()

    def add_segment(
        self,
        start: QPointF,
        end: QPointF,
        start_pressure: float,
        end_pressure: float,
    ) -> None:
        # Smooth the already de-duplicated input very lightly, then sample by
        # arc length. This hides event-boundary kinks without changing the path.
        raw_length = math.hypot(end.x() - start.x(), end.y() - start.y())
        if raw_length > max(3.0, self._size * 0.5):
            smooth_start = QPointF(start)
            smooth_end = QPointF(end)
        else:
            smooth_start = QPointF(start) if self._smoothed_point is None else QPointF(self._smoothed_point)
            smoothing = 0.74
            smooth_end = QPointF(
                smooth_start.x() + (end.x() - smooth_start.x()) * smoothing,
                smooth_start.y() + (end.y() - smooth_start.y()) * smoothing,
            )
        self._smoothed_point = QPointF(smooth_end)
        start = smooth_start
        end = smooth_end
        length = math.hypot(end.x() - start.x(), end.y() - start.y())
        if length <= 0.001:
            return
        # Dense, arc-length based samples keep the graphite material continuous
        # regardless of how sparsely the input device delivers move events.
        spacing = max(0.18, self._size * 0.055)
        steps = max(1, math.ceil(length / spacing))
        tangent = math.atan2(end.y() - start.y(), end.x() - start.x())
        previous_point: QPointF | None = None
        previous_pressure = start_pressure
        previous_phase = self._distance
        for step in range(steps + 1):
            t = step / steps
            point = QPointF(
                start.x() + (end.x() - start.x()) * t,
                start.y() + (end.y() - start.y()) * t,
            )
            pressure = start_pressure + (end_pressure - start_pressure) * t
            phase = self._distance + length * t
            if previous_point is not None:
                self._deposit_bridge(
                    previous_point,
                    point,
                    previous_pressure,
                    pressure,
                    previous_phase,
                    phase,
                )
            self._stamp(point, tangent, pressure, phase)
            previous_point = point
            previous_pressure = pressure
            previous_phase = phase
        self._distance += length

    def add_dot(self, point: QPointF, pressure: float) -> None:
        self._stamp(point, 0.0, pressure, self._distance)

    def _tip_parameters(self, pressure: float) -> tuple[float, float]:
        pressure = max(0.05, min(1.0, pressure))
        size_scale = 0.45 + 0.55 * pressure if self._pressure_size else 1.0
        density_scale = 0.28 + 0.72 * pressure if self._pressure_density else 1.0
        return (
            max(1.0, self._size * size_scale),
            max(0.04, min(1.0, self._density * density_scale)),
        )

    def _deposit_bridge(
        self,
        start: QPointF,
        end: QPointF,
        start_pressure: float,
        end_pressure: float,
        start_phase: float,
        end_phase: float,
    ) -> None:
        self._ensure_mask()
        if self._mask.isNull():
            return
        start_diameter, start_density = self._tip_parameters(start_pressure)
        end_diameter, end_density = self._tip_parameters(end_pressure)
        midpoint_density = (start_density + end_density) / 2
        midpoint_diameter = (start_diameter + end_diameter) / 2
        phase_strength = 0.92 + 0.08 * math.sin((start_phase + end_phase) * 0.04)
        alpha = round(255 * (0.05 + midpoint_density * 0.17) * phase_strength)
        width = max(0.7, midpoint_diameter * (0.30 + 0.10 * midpoint_density))

        painter = QPainter(self._mask)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Lighten)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(
            QPen(
                QColor(255, 255, 255, max(1, min(255, alpha))),
                width,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.drawLine(start, end)
        painter.end()

        radius = max(start_diameter, end_diameter) / 2 + 2
        self._mark_segment_dirty(start, end, radius)

    def render_colored(self, target: QImage, color: QColor) -> None:
        if self._mask.isNull():
            return
        render_key = (color.rgba(), False)
        if render_key != self._last_render_key:
            self._dirty_rect = target.rect()
            self._last_render_key = render_key
        rect = self._dirty_rect.intersected(target.rect())
        if rect.isEmpty():
            return
        painter = QPainter(target)
        painter.setClipRect(rect)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(rect, Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.fillRect(rect, color)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawImage(0, 0, self._mask)
        painter.end()
        self._dirty_rect = QRect()

    def _ensure_mask(self) -> None:
        if not self._mask.isNull():
            return
        self._mask = QImage(self._target_size, QImage.Format.Format_ARGB32_Premultiplied)
        self._mask.fill(Qt.GlobalColor.transparent)

    def _stamp(self, point: QPointF, tangent: float, pressure: float, phase: float) -> None:
        self._ensure_mask()
        if self._mask.isNull():
            return
        diameter, density = self._tip_parameters(pressure)
        tip = self._material_tip(diameter, density)

        phase_angle = math.sin(phase * 0.055) * self._tip_variation * 0.16
        phase_offset = math.sin(phase * 0.083 + 1.7) * self._tip_variation * diameter * 0.06
        normal_x = -math.sin(tangent)
        normal_y = math.cos(tangent)
        center = QPointF(point.x() + normal_x * phase_offset, point.y() + normal_y * phase_offset)
        logical_width = tip.width() / tip.devicePixelRatio()
        logical_height = tip.height() / tip.devicePixelRatio()

        painter = QPainter(self._mask)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Lighten)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.save()
        painter.translate(center)
        painter.rotate(math.degrees(phase_angle))
        painter.drawImage(QPointF(-logical_width / 2, -logical_height / 2), tip)
        painter.restore()
        painter.end()

        self._mark_segment_dirty(center, center, max(logical_width, logical_height) / 2 + 2)

    def _mark_segment_dirty(self, start: QPointF, end: QPointF, radius: float) -> None:
        left = max(0, math.floor(min(start.x(), end.x()) - radius))
        top = max(0, math.floor(min(start.y(), end.y()) - radius))
        right = min(self._mask.width(), math.ceil(max(start.x(), end.x()) + radius))
        bottom = min(self._mask.height(), math.ceil(max(start.y(), end.y()) + radius))
        rect = QRect(left, top, max(0, right - left), max(0, bottom - top))
        if not rect.isEmpty():
            self._dirty_rect = rect if self._dirty_rect.isNull() else self._dirty_rect.united(rect)

    def _material_tip(self, diameter: float, density: float) -> QImage:
        dpr = 1.0
        size_key = max(2, round(diameter * dpr))
        density_key = max(1, min(20, round(density * 20)))
        key = (size_key, self._grain, density_key, round(dpr * 100))
        cached = self._tip_cache.get(key)
        if cached is not None:
            return cached

        image = QImage(size_key, size_key, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        radius = size_key / 2
        grain = self._grain / 100
        effective_density = density_key / 20
        for y in range(size_key):
            for x in range(size_key):
                dx = x + 0.5 - radius
                dy = y + 0.5 - radius
                normalized = math.hypot(dx, dy) / max(0.001, radius)
                if normalized >= 1.0:
                    continue
                edge = 1.0 - normalized
                edge = edge * edge * (3.0 - 2.0 * edge)
                coarse = 0.5 + 0.5 * math.sin(x * 0.71 + y * 1.19 + math.sin(x * 0.13))
                medium = 0.5 + 0.5 * math.sin(x * 2.37 - y * 1.61 + 0.9)
                fine = 0.5 + 0.5 * math.sin(x * 7.17 + y * 5.31 + math.sin(y * 0.49))
                graphite = 0.58 + (coarse - 0.5) * 0.20 * grain + (medium - 0.5) * 0.36 * grain
                porosity = effective_density - (fine - 0.5) * grain * 0.62
                deposit = max(0.0, min(1.0, porosity * 1.55))
                alpha = round(255 * edge * graphite * deposit)
                if alpha:
                    image.setPixelColor(x, y, QColor(255, 255, 255, alpha))
        image.setDevicePixelRatio(dpr)
        self._tip_cache[key] = image
        return image


class DrawingGraphicsView(QGraphicsView):
    drawing_changed = Signal(str, QRect, QImage, QImage)
    stroke_started = Signal(QImage)
    drawing_attempted = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._drawing_item: QGraphicsPixmapItem | None = None
        self._drawing_previous_item: QGraphicsPixmapItem | None = None
        self._drawing_next_item: QGraphicsPixmapItem | None = None
        self._stroke_preview_item: QGraphicsPixmapItem | None = None
        self._drawing_previous_source_pixmap = QPixmap()
        self._drawing_next_source_pixmap = QPixmap()
        self._drawing_image = QImage()
        self._stroke_preview_image = QImage()
        self._square_stroke_mask = QImage()
        self._drawing_enabled = False
        self._drawing_blocked = False
        self._drawing_tool = "brush"
        self._brush_color = QColor("#000000")
        self._brush_opacity = 1.0
        self._brush_hardness = 20
        self._brush_size = 4
        self._soft_edge_gamma = 0.75
        self._soft_flow = 0.25
        self._soft_renderer_mode = "softRound"
        self._soft_debug_mode = "off"
        self._soft_pressure_size = True
        self._soft_pressure_opacity = False
        self._soft_renderer = SoftBrushRenderer()
        self._square_tip_cache: dict[tuple[int, int], QImage] = {}
        self._pencil_grain = 55
        self._pencil_density = 68
        self._pencil_pressure_size = True
        self._pencil_pressure_density = True
        self._pencil_tip_variation = 20
        self._pencil_renderer = PencilRenderer()
        self._square_brush_follow_path = True
        self._square_brush_fixed_angle = math.radians(45)
        self._square_pressure_size_enabled = True
        self._square_pressure_size_strength = 0.22
        self._square_min_pressure_size = 0.72
        self._last_square_angle: float | None = None
        self._is_drawing = False
        self._last_draw_point = QPointF()
        self._last_input_point = QPointF()
        self._last_draw_pressure = 1.0
        self._last_input_pressure = 1.0
        self._press_pressure = 1.0
        self._filtered_pressure = 1.0
        self._previous_point = QPointF()
        self._midpoint = QPointF()
        self._control_point = QPointF()
        self._last_move_timestamp = -1
        self._press_point: QPointF | None = None
        self._waiting_for_first_motion = False
        self._stroke_points: list[QPointF] = []
        self._stroke_path = QPainterPath()
        self._stroke_dot_points: list[QPointF] = []
        self._square_stroke_stamps: list[tuple[QPointF, float, float]] = []
        self._stroke_dirty_rect = QRect()
        self._gradient_start: QPointF | None = None
        self._gradient_end = QPointF()
        self._gradient_end_color = QColor("#00000000")
        self._stroke_has_content = False
        self._cursor_scene_pos: QPointF | None = None
        self._cursor_in_bounds = False
        self._drawing_dirty = False
        self._drawing_preview_timer = QTimer(self)
        self._drawing_preview_timer.setSingleShot(True)
        self._drawing_preview_timer.timeout.connect(self._flush_drawing_preview)
        self._drawing_onion_enabled = False
        self._drawing_previous_onion_opacity = 0.3
        self._drawing_next_onion_opacity = 0.2
        self._drawing_previous_onion_color = QColor("#ef4444")
        self._drawing_next_onion_color = QColor("#3b82f6")
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    def _init_drawing_layer(self, z_value: int) -> None:
        self._drawing_previous_item = QGraphicsPixmapItem()
        self._drawing_next_item = QGraphicsPixmapItem()
        self._drawing_item = QGraphicsPixmapItem()
        self._stroke_preview_item = QGraphicsPixmapItem()
        self._drawing_previous_item.setZValue(z_value - 2)
        self._drawing_next_item.setZValue(z_value - 1)
        self._drawing_item.setZValue(z_value)
        self._stroke_preview_item.setZValue(z_value + 1)
        self.scene().addItem(self._drawing_previous_item)
        self.scene().addItem(self._drawing_next_item)
        self.scene().addItem(self._drawing_item)
        self.scene().addItem(self._stroke_preview_item)
        self._stroke_preview_item.hide()

    def set_drawing_blocked(self, blocked: bool) -> None:
        self._drawing_blocked = blocked
        if blocked:
            self._cancel_stroke_state()

    def set_drawing_onion_skin_enabled(self, enabled: bool) -> None:
        self._drawing_onion_enabled = enabled
        self._sync_drawing_onion_state()

    def set_drawing_onion_opacity(self, opacity: float) -> None:
        self._drawing_previous_onion_opacity = max(0.0, min(0.3, opacity))
        self._drawing_next_onion_opacity = max(0.0, min(0.22, opacity * 0.72))
        self._sync_drawing_onion_state()

    def set_drawing_previous_onion_color(self, color: QColor | str) -> None:
        self._drawing_previous_onion_color = QColor(color)
        self._refresh_drawing_onion_pixmaps()

    def set_drawing_next_onion_color(self, color: QColor | str) -> None:
        self._drawing_next_onion_color = QColor(color)
        self._refresh_drawing_onion_pixmaps()

    def set_drawing_onion_layers(
        self,
        previous_image: QImage | None,
        next_image: QImage | None,
    ) -> None:
        if self._drawing_previous_item is None or self._drawing_next_item is None:
            return
        self._drawing_previous_source_pixmap = QPixmap.fromImage(previous_image) if previous_image is not None else QPixmap()
        self._drawing_next_source_pixmap = QPixmap.fromImage(next_image) if next_image is not None else QPixmap()
        self._drawing_previous_item.setPixmap(
            self._tint_drawing_pixmap(self._drawing_previous_source_pixmap, self._drawing_previous_onion_color)
        )
        self._drawing_next_item.setPixmap(
            self._tint_drawing_pixmap(self._drawing_next_source_pixmap, self._drawing_next_onion_color)
        )
        self._sync_drawing_onion_state()

    def set_drawing_enabled(self, enabled: bool) -> None:
        if not enabled:
            self._cancel_stroke_state()
        self._drawing_enabled = enabled
        if not enabled:
            self._cursor_scene_pos = None
            self._cursor_in_bounds = False
            self.viewport().unsetCursor()
        self.viewport().update()

    def set_drawing_tool(self, tool: str) -> None:
        if tool in ("brush", "soft_brush", "square_brush", "pencil", "eraser", "bucket", "gradient"):
            self._finish_active_stroke(emit_change=True)
            self._cancel_gradient()
            self._drawing_tool = tool
            self.viewport().update()

    def set_brush_color(self, color: QColor | str) -> None:
        self._brush_color = QColor(color)
        self.viewport().update()

    def set_brush_opacity(self, opacity: float) -> None:
        self._brush_opacity = max(0.0, min(1.0, opacity))
        if self._stroke_preview_item is not None and self._drawing_tool != "eraser":
            self._stroke_preview_item.setOpacity(self._brush_opacity)
        self.viewport().update()

    def set_brush_size(self, size: int) -> None:
        self._brush_size = max(1, size)
        self.viewport().update()

    def set_brush_hardness(self, hardness: int) -> None:
        self._brush_hardness = max(0, min(100, hardness))
        self.viewport().update()

    def set_soft_edge_gamma(self, gamma: float) -> None:
        self._soft_edge_gamma = max(0.1, min(8.0, float(gamma)))
        self.viewport().update()

    def set_soft_flow(self, flow: float) -> None:
        self._soft_flow = max(0.01, min(1.0, float(flow)))
        self.viewport().update()

    def set_soft_renderer_mode(self, mode: str) -> None:
        self._finish_active_stroke(emit_change=True)
        self._soft_renderer_mode = "airbrush" if mode == "airbrush" else "softRound"
        self.viewport().update()

    def set_soft_debug_mode(self, mode: str) -> None:
        self._soft_debug_mode = mode
        self.viewport().update()

    def set_soft_pressure_size_enabled(self, enabled: bool) -> None:
        self._soft_pressure_size = enabled

    def set_soft_pressure_opacity_enabled(self, enabled: bool) -> None:
        self._soft_pressure_opacity = enabled

    def set_square_pressure_size_enabled(self, enabled: bool) -> None:
        self._square_pressure_size_enabled = enabled

    def set_square_pressure_size_strength(self, strength: float) -> None:
        self._square_pressure_size_strength = max(0.0, min(0.5, float(strength)))

    def set_square_min_pressure_size(self, minimum: float) -> None:
        self._square_min_pressure_size = max(0.3, min(1.0, float(minimum)))

    def set_pencil_grain(self, grain: int) -> None:
        self._pencil_grain = max(0, min(100, int(grain)))
        self._pencil_renderer.clear_tip_cache()
        self.viewport().update()

    def set_pencil_density(self, density: int) -> None:
        self._pencil_density = max(5, min(100, int(density)))
        self.viewport().update()

    def set_pencil_pressure_size_enabled(self, enabled: bool) -> None:
        self._pencil_pressure_size = enabled
        self.viewport().update()

    def set_pencil_pressure_density_enabled(self, enabled: bool) -> None:
        self._pencil_pressure_density = enabled
        self.viewport().update()

    def set_pencil_tip_variation(self, variation: int) -> None:
        self._pencil_tip_variation = max(0, min(100, int(variation)))
        self.viewport().update()

    def set_gradient_end_color(self, color: QColor | str) -> None:
        self._gradient_end_color = QColor(color)
        self.viewport().update()

    def set_square_brush_angle_mode(self, follow_path: bool) -> None:
        self._square_brush_follow_path = follow_path
        self.viewport().update()

    def set_drawing_image(self, image: QImage) -> None:
        self._cancel_stroke_state()
        self._drawing_image = image.copy()
        self._refresh_drawing_item()

    def set_drawing_layer_size(self, width: int, height: int) -> None:
        self._cancel_stroke_state()
        width = max(1, width)
        height = max(1, height)
        if self._drawing_image.size() == QSize(width, height):
            self._refresh_drawing_item()
            return

        resized = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        resized.fill(Qt.GlobalColor.transparent)
        if not self._drawing_image.isNull():
            painter = QPainter(resized)
            painter.drawImage(0, 0, self._drawing_image)
            painter.end()
        self._drawing_image = resized
        self._refresh_drawing_item()

    def drawing_image_copy(self) -> QImage:
        return self._drawing_image.copy()

    def apply_drawing_patch(self, rect: QRect, patch: QImage) -> None:
        if self._drawing_image.isNull() or patch.isNull():
            return
        target_rect = rect.intersected(self._drawing_image.rect())
        if target_rect.isEmpty():
            return
        source_rect = QRect(
            target_rect.x() - rect.x(),
            target_rect.y() - rect.y(),
            target_rect.width(),
            target_rect.height(),
        )
        painter = QPainter(self._drawing_image)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.drawImage(target_rect.topLeft(), patch.copy(source_rect))
        painter.end()
        self._refresh_drawing_item()

    def clear_drawing(self) -> None:
        if self._drawing_image.isNull():
            return
        self._cancel_stroke_state()
        rect = self._drawing_image.rect()
        before_patch = self._drawing_image.copy(rect)
        self._drawing_image.fill(Qt.GlobalColor.transparent)
        self._refresh_drawing_item()
        self.drawing_changed.emit("clear", rect, before_patch, self._drawing_image.copy(rect))

    def export_drawing_png(self, path: str) -> bool:
        if self._drawing_image.isNull():
            return False
        return self._drawing_image.save(path, "PNG")

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        self._update_drawing_cursor(event.position())
        if self._drawing_enabled and self._drawing_blocked and event.button() == Qt.MouseButton.LeftButton:
            self.drawing_attempted.emit()
        if self._should_handle_drawing(event):
            scene_pos = self._scene_point_from_viewport(event.position())
            if self._drawing_bounds_rect().contains(scene_pos):
                point = self._clamped_point(scene_pos)
                if self._drawing_tool == "bucket":
                    self._apply_bucket(point)
                    event.accept()
                    return
                if self._drawing_tool == "gradient":
                    self._begin_gradient(point)
                    event.accept()
                    return
                self._begin_stroke(point, self._smoothed_event_pressure(event, reset=True))
                self._is_drawing = True
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        self._update_drawing_cursor(event.position())
        if self._is_drawing:
            if self._drawing_tool == "gradient":
                self._update_gradient_preview(
                    self._clamped_point(self._scene_point_from_viewport(event.position()))
                )
                event.accept()
                return
            if not self._accept_move_event(event):
                event.accept()
                return
            scene_pos = self._scene_point_from_viewport(event.position())
            if self._is_abnormal_jump(scene_pos):
                event.accept()
                return
            self._draw_stroke(self._last_draw_point, scene_pos, self._smoothed_event_pressure(event))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        release_start = perf_counter()
        if self._is_drawing and event.button() == Qt.MouseButton.LeftButton:
            if self._drawing_tool == "gradient":
                self._commit_gradient(
                    self._clamped_point(self._scene_point_from_viewport(event.position()))
                )
                self._log_release_profile(release_start, "gradient")
                event.accept()
                return
            self._append_input_point(
                self._clamped_point(self._scene_point_from_viewport(event.position())),
                pressure=self._smoothed_event_pressure(event),
                force=True,
            )
            self._finish_active_stroke(emit_change=True, release_start=release_start)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._finish_active_stroke(emit_change=True)
        self._cursor_scene_pos = None
        self._cursor_in_bounds = False
        self.viewport().unsetCursor()
        self.viewport().update()
        super().leaveEvent(event)

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        self._finish_active_stroke(emit_change=True)
        super().focusOutEvent(event)

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # type: ignore[override]
        super().drawForeground(painter, rect)
        if self._is_drawing and self._drawing_tool == "soft_brush" and self._soft_debug_mode in {
            "raw",
            "resampled",
            "capsules",
        }:
            self._soft_renderer.draw_debug(painter, self._soft_debug_mode)
        if not self._drawing_enabled or not self._cursor_in_bounds or self._cursor_scene_pos is None:
            return

        zoom = max(0.001, abs(self.transform().m11()))
        cursor_color = QColor("#ef4444") if self._drawing_tool == "eraser" else QColor(self._brush_color)
        cursor_color.setAlpha(220)
        painter.save()
        painter.setPen(QPen(cursor_color, 0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        radius = max(self._brush_size / 2, 1.5)
        x = self._cursor_scene_pos.x()
        y = self._cursor_scene_pos.y()
        if self._drawing_tool == "square_brush":
            painter.drawRect(QRectF(x - radius * 1.2, y - radius * 0.7, radius * 2.4, radius * 1.4))
        else:
            painter.drawEllipse(self._cursor_scene_pos, radius, radius)
        arm = 7 / zoom
        gap = radius + 2 / zoom
        painter.drawLine(QPointF(x - gap - arm, y), QPointF(x - gap, y))
        painter.drawLine(QPointF(x + gap, y), QPointF(x + gap + arm, y))
        painter.drawLine(QPointF(x, y - gap - arm), QPointF(x, y - gap))
        painter.drawLine(QPointF(x, y + gap), QPointF(x, y + gap + arm))
        painter.restore()

    def _should_handle_drawing(self, event: QMouseEvent) -> bool:
        return (
            self._drawing_enabled
            and not self._drawing_blocked
            and event.button() == Qt.MouseButton.LeftButton
        )

    def _drawing_bounds_rect(self) -> QRectF:
        return QRectF(0, 0, self._drawing_image.width(), self._drawing_image.height())

    def _draw_stroke(self, start: QPointF, end: QPointF, pressure: float) -> None:
        if self._drawing_image.isNull():
            return

        start = self._clamped_point(start)
        end = self._clamped_point(end)
        if self._press_point is None:
            self._begin_stroke(start)
        self._append_input_point(end, pressure=pressure)

    def _begin_stroke(self, point: QPointF, pressure: float = 1.0) -> None:
        self._cancel_stroke_state()
        self._create_stroke_preview_layer()
        self._last_draw_point = QPointF()
        self._last_input_point = point
        self._last_draw_pressure = pressure
        self._last_input_pressure = pressure
        self._press_pressure = pressure
        self._filtered_pressure = pressure
        self._stroke_points = []
        self._stroke_path = QPainterPath()
        self._stroke_dot_points.clear()
        self._square_stroke_stamps.clear()
        self._stroke_dirty_rect = QRect()
        self._last_square_angle = None
        self._press_point = point
        self._waiting_for_first_motion = True
        self._stroke_has_content = False
        if self._drawing_tool == "soft_brush":
            self._soft_renderer.set_canvas_size(self._drawing_image.size())
            self._soft_renderer.begin(
                self._brush_size,
                self._brush_hardness,
                self._soft_edge_gamma,
                self._soft_flow,
                self._soft_renderer_mode,
                point,
            )
        elif self._drawing_tool == "pencil":
            self._pencil_renderer.set_canvas_size(self._drawing_image.size())
            self._pencil_renderer.begin(
                self._brush_size,
                self._pencil_grain,
                self._pencil_density / 100,
                self._pencil_pressure_size,
                self._pencil_pressure_density,
                self._pencil_tip_variation / 100,
            )

    def _append_input_point(self, point: QPointF, *, pressure: float = 1.0, force: bool = False) -> None:
        point = self._clamped_point(point)
        pressure = max(0.05, min(1.0, pressure))
        if self._drawing_tool == "soft_brush":
            self._soft_renderer.add_raw_point(point)
        distance = math.hypot(
            point.x() - self._last_input_point.x(),
            point.y() - self._last_input_point.y(),
        )
        minimum_distance = max(0.05, self._resample_spacing() * 0.1)
        if distance < minimum_distance:
            return

        # Pen press coordinates can precede the first real motion sample.
        # The first move creates the only path start; it is not duplicated.
        if self._waiting_for_first_motion:
            start = self._press_point or point
            self._stroke_path.moveTo(start)
            self._stroke_path.lineTo(point)
            self._previous_point = start
            self._last_draw_point = point
            self._last_input_point = point
            self._stroke_points = [point]
            self._waiting_for_first_motion = False
            self._queue_linear_segment(
                start,
                point,
                start_pressure=self._press_pressure,
                end_pressure=pressure,
                path_already_started=True,
            )
            self._last_draw_pressure = pressure
            self._last_input_pressure = pressure
            return

        if not force and distance < self._resample_spacing() * 0.35:
            return

        steps = max(1, math.ceil(distance / self._resample_spacing()))
        origin = self._last_input_point
        origin_pressure = self._last_input_pressure
        for step in range(1, steps + 1):
            ratio = step / steps
            sample = QPointF(
                origin.x() + (point.x() - origin.x()) * ratio,
                origin.y() + (point.y() - origin.y()) * ratio,
            )
            sample_pressure = origin_pressure + (pressure - origin_pressure) * ratio
            self._append_smoothed_point(sample, sample_pressure)
        self._last_input_point = point
        self._last_input_pressure = pressure

    def _append_smoothed_point(self, point: QPointF, pressure: float) -> None:
        if self._stroke_points and self._points_are_close(self._stroke_points[-1], point):
            return

        self._queue_linear_segment(
            self._last_draw_point,
            point,
            start_pressure=self._last_draw_pressure,
            end_pressure=pressure,
        )
        self._previous_point = self._last_draw_point
        self._last_draw_point = point
        self._last_draw_pressure = pressure
        self._stroke_points = [point]

    def _queue_linear_segment(
        self,
        start: QPointF,
        end: QPointF,
        *,
        start_pressure: float = 1.0,
        end_pressure: float = 1.0,
        path_already_started: bool = False,
    ) -> None:
        if self._points_are_close(start, end):
            return
        if self._stroke_path.isEmpty():
            self._stroke_path.moveTo(start)
        if not path_already_started:
            self._stroke_path.lineTo(end)
        if self._drawing_tool == "soft_brush":
            self._soft_renderer.add_capsule(
                start,
                end,
                self._soft_radius_for_pressure(start_pressure),
                self._soft_radius_for_pressure(end_pressure),
                self._soft_strength_for_pressure(start_pressure),
                self._soft_strength_for_pressure(end_pressure),
            )
        elif self._drawing_tool == "square_brush":
            self._append_square_line_stamps(start, end, start_pressure, end_pressure)
        elif self._drawing_tool == "pencil":
            self._pencil_renderer.add_segment(start, end, start_pressure, end_pressure)
        self._mark_stroke_dirty(start, end)
        self._stroke_has_content = True
        self._schedule_drawing_preview()

    def _queue_quadratic_segment(self, start: QPointF, control: QPointF, end: QPointF) -> None:
        if self._points_are_close(start, end):
            return
        if self._stroke_path.isEmpty():
            self._stroke_path.moveTo(start)
        self._stroke_path.quadTo(control, end)
        if self._drawing_tool == "soft_brush":
            self._soft_renderer.add_capsule(start, end)
        elif self._drawing_tool == "square_brush":
            self._append_square_curve_stamps(start, control, end)
        elif self._drawing_tool == "pencil":
            self._pencil_renderer.add_segment(start, end, 1.0, 1.0)
        self._mark_stroke_dirty(start, end)
        self._stroke_has_content = True
        self._schedule_drawing_preview()

    def _queue_dot(self, point: QPointF) -> None:
        self._stroke_dot_points = [point]
        if self._drawing_tool == "soft_brush":
            self._soft_renderer.add_dot(
                point,
                self._soft_radius_for_pressure(self._press_pressure),
                self._soft_strength_for_pressure(self._press_pressure),
            )
        elif self._drawing_tool == "square_brush":
            self._square_stroke_stamps = [
                (point, self._square_brush_fixed_angle, self._square_scale_for_pressure(self._press_pressure))
            ]
        elif self._drawing_tool == "pencil":
            self._pencil_renderer.add_dot(point, self._press_pressure)
        self._mark_stroke_dirty(point, point)
        self._stroke_has_content = True
        self._schedule_drawing_preview()

    def _mark_stroke_dirty(self, start: QPointF, end: QPointF) -> None:
        pad = self._dirty_padding()
        left = math.floor(min(start.x(), end.x()) - pad)
        top = math.floor(min(start.y(), end.y()) - pad)
        right = math.ceil(max(start.x(), end.x()) + pad)
        bottom = math.ceil(max(start.y(), end.y()) + pad)
        rect = QRect(left, top, max(1, right - left + 1), max(1, bottom - top + 1))
        rect = rect.intersected(self._drawing_image.rect())
        if rect.isEmpty():
            return
        self._stroke_dirty_rect = rect if self._stroke_dirty_rect.isNull() else self._stroke_dirty_rect.united(rect)

    def _dirty_padding(self) -> float:
        if self._drawing_tool == "square_brush":
            return self._brush_size * 1.2 + 4
        return self._brush_size / 2 + 4

    def _schedule_drawing_preview(self) -> None:
        self._drawing_dirty = True
        if not self._drawing_preview_timer.isActive():
            self._drawing_preview_timer.start(16)

    def _finish_active_stroke(self, *, emit_change: bool, release_start: float | None = None) -> None:
        if self._gradient_start is not None:
            self._cancel_gradient()
            return
        if not self._is_drawing and self._press_point is None:
            return

        self._is_drawing = False
        if not self._stroke_points:
            self._queue_dot(self._press_point or QPointF())
        else:
            final_point = self._stroke_points[-1]
            if not self._points_are_close(self._last_draw_point, final_point):
                self._queue_quadratic_segment(
                    self._last_draw_point,
                    final_point,
                    final_point,
                )
                self._last_draw_point = final_point

        flush_start = perf_counter()
        self._flush_drawing_preview()
        flush_ms = (perf_counter() - flush_start) * 1000
        changed = self._stroke_has_content
        commit_ms = 0.0
        emit_ms = 0.0
        committed: tuple[str, QRect, QImage, QImage] | None = None
        if changed:
            commit_start = perf_counter()
            committed = self._commit_stroke_preview()
            commit_ms = (perf_counter() - commit_start) * 1000
        self._clear_stroke_preview_layer()
        self._reset_stroke_tracking()
        if committed is not None and emit_change:
            emit_start = perf_counter()
            self.drawing_changed.emit(*committed)
            emit_ms = (perf_counter() - emit_start) * 1000
        if release_start is not None:
            total_ms = (perf_counter() - release_start) * 1000
            rect_text = "none"
            if committed is not None:
                rect = committed[1]
                rect_text = f"{rect.width()}x{rect.height()}"
            if total_ms >= 16.0:
                print(
                    "[stroke-release] "
                    f"tool={self._drawing_tool} rect={rect_text} "
                    f"preview_flush={flush_ms:.2f}ms stroke_commit={commit_ms:.2f}ms "
                    f"main_window_handlers={emit_ms:.2f}ms mouseReleaseEvent={total_ms:.2f}ms"
                )

    def _log_release_profile(self, release_start: float, tool: str) -> None:
        total_ms = (perf_counter() - release_start) * 1000
        if total_ms >= 16.0:
            print(f"[stroke-release] tool={tool} mouseReleaseEvent={total_ms:.2f}ms")

    def _paint_pending_stroke(self) -> None:
        if self._stroke_path.isEmpty() and not self._stroke_dot_points:
            return

        if self._stroke_preview_image.isNull():
            return

        if self._drawing_tool == "soft_brush":
            self._rebuild_soft_stroke_preview()
            self._refresh_stroke_preview_item()
            return
        if self._drawing_tool == "square_brush":
            self._rebuild_square_stroke_preview()
            self._refresh_stroke_preview_item()
            return
        if self._drawing_tool == "pencil":
            self._pencil_renderer.render_colored(self._stroke_preview_image, self._brush_color)
            self._refresh_stroke_preview_item()
            return

        self._stroke_preview_image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(self._stroke_preview_image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._drawing_tool == "eraser":
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            pen = QPen(Qt.GlobalColor.white, self._brush_size, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            dot_brush = QColor(Qt.GlobalColor.white)
        else:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            brush_color = QColor(self._brush_color)
            brush_color.setAlpha(255)
            pen = QPen(brush_color, self._brush_size, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            dot_brush = brush_color
        painter.setPen(pen)
        if not self._stroke_path.isEmpty():
            painter.drawPath(self._stroke_path)
        if self._stroke_dot_points:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(dot_brush)
            radius = self._brush_size / 2
            for point in self._stroke_dot_points:
                painter.drawEllipse(point, radius, radius)
        painter.end()

        self._refresh_stroke_preview_item()

    def _clamped_point(self, point: QPointF) -> QPointF:
        bounds = self._drawing_bounds_rect()
        return QPointF(
            min(max(point.x(), bounds.left()), bounds.right()),
            min(max(point.y(), bounds.top()), bounds.bottom()),
        )

    def _apply_bucket(self, point: QPointF) -> None:
        if self._drawing_image.isNull():
            return
        x = int(point.x())
        y = int(point.y())
        if not self._drawing_image.rect().contains(x, y):
            return
        before = self._drawing_image.copy()
        fill_color = QColor(self._brush_color)
        fill_color.setAlpha(round(255 * self._brush_opacity))
        changed_rect = self._flood_fill(x, y, fill_color)
        if changed_rect.isNull():
            return
        before_patch = before.copy(changed_rect)
        self._refresh_drawing_item()
        self.drawing_changed.emit("bucket", changed_rect, before_patch, self._drawing_image.copy(changed_rect))

    def _flood_fill(self, start_x: int, start_y: int, fill_color: QColor) -> QRect:
        target = self._drawing_image.pixelColor(start_x, start_y)
        if self._colors_are_close(target, fill_color):
            return QRect()

        width = self._drawing_image.width()
        height = self._drawing_image.height()
        stack = [(start_x, start_y)]
        tolerance = 8
        left_bound = width
        right_bound = -1
        top_bound = height
        bottom_bound = -1
        while stack:
            x, y = stack.pop()
            if y < 0 or y >= height or x < 0 or x >= width:
                continue
            if not self._colors_are_close(self._drawing_image.pixelColor(x, y), target, tolerance):
                continue

            left = x
            while left > 0 and self._colors_are_close(self._drawing_image.pixelColor(left - 1, y), target, tolerance):
                left -= 1
            right = x
            while right + 1 < width and self._colors_are_close(self._drawing_image.pixelColor(right + 1, y), target, tolerance):
                right += 1

            for fill_x in range(left, right + 1):
                self._drawing_image.setPixelColor(fill_x, y, fill_color)
            left_bound = min(left_bound, left)
            right_bound = max(right_bound, right)
            top_bound = min(top_bound, y)
            bottom_bound = max(bottom_bound, y)

            for neighbor_y in (y - 1, y + 1):
                if neighbor_y < 0 or neighbor_y >= height:
                    continue
                fill_x = left
                while fill_x <= right:
                    while fill_x <= right and not self._colors_are_close(
                        self._drawing_image.pixelColor(fill_x, neighbor_y), target, tolerance
                    ):
                        fill_x += 1
                    if fill_x > right:
                        break
                    stack.append((fill_x, neighbor_y))
                    while fill_x <= right and self._colors_are_close(
                        self._drawing_image.pixelColor(fill_x, neighbor_y), target, tolerance
                    ):
                        fill_x += 1
        if right_bound < left_bound or bottom_bound < top_bound:
            return QRect()
        return QRect(left_bound, top_bound, right_bound - left_bound + 1, bottom_bound - top_bound + 1)

    @staticmethod
    def _colors_are_close(first: QColor, second: QColor, tolerance: int = 0) -> bool:
        return (
            abs(first.red() - second.red()) <= tolerance
            and abs(first.green() - second.green()) <= tolerance
            and abs(first.blue() - second.blue()) <= tolerance
            and abs(first.alpha() - second.alpha()) <= tolerance
        )

    def _begin_gradient(self, point: QPointF) -> None:
        self._cancel_stroke_state()
        self._create_stroke_preview_layer()
        self._gradient_start = QPointF(point)
        self._gradient_end = QPointF(point)
        self._is_drawing = True
        self._render_gradient_preview()

    def _update_gradient_preview(self, point: QPointF) -> None:
        if self._gradient_start is None:
            return
        self._gradient_end = QPointF(point)
        self._render_gradient_preview()

    def _render_gradient_preview(self) -> None:
        if self._gradient_start is None or self._stroke_preview_image.isNull():
            return
        self._stroke_preview_image.fill(Qt.GlobalColor.transparent)
        start_color = QColor(self._brush_color)
        start_color.setAlpha(255)
        end_color = QColor(self._gradient_end_color)
        if not end_color.isValid():
            end_color = QColor(0, 0, 0, 0)
        if self._colors_are_close(start_color, end_color) and self._points_are_close(
            self._gradient_start, self._gradient_end
        ):
            end_color = QColor(start_color)
        gradient = QLinearGradient(self._gradient_start, self._gradient_end)
        gradient.setColorAt(0.0, start_color)
        gradient.setColorAt(1.0, end_color)
        painter = QPainter(self._stroke_preview_image)
        painter.fillRect(self._stroke_preview_image.rect(), gradient)
        painter.end()
        self._refresh_stroke_preview_item()

    def _commit_gradient(self, point: QPointF) -> None:
        if self._gradient_start is None or self._drawing_image.isNull():
            self._cancel_gradient()
            return
        self._gradient_end = QPointF(point)
        self._render_gradient_preview()
        rect = self._drawing_image.rect()
        before_patch = self._drawing_image.copy(rect)
        painter = QPainter(self._drawing_image)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setOpacity(self._brush_opacity)
        painter.drawImage(0, 0, self._stroke_preview_image)
        painter.end()
        self._refresh_drawing_item()
        self._clear_stroke_preview_layer()
        self._gradient_start = None
        self._is_drawing = False
        self.drawing_changed.emit("gradient", rect, before_patch, self._drawing_image.copy(rect))

    def _cancel_gradient(self) -> None:
        self._gradient_start = None
        self._is_drawing = False
        self._clear_stroke_preview_layer()

    def _refresh_drawing_item(self) -> None:
        if self._drawing_item is None:
            return
        self._drawing_item.setPixmap(QPixmap.fromImage(self._drawing_image))
        self.viewport().update()

    def _create_stroke_preview_layer(self) -> None:
        if self._drawing_image.isNull() or self._stroke_preview_item is None:
            return
        self._stroke_preview_image = QImage(
            self._drawing_image.size(),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        self._stroke_preview_image.fill(Qt.GlobalColor.transparent)
        self._square_stroke_mask = QImage(
            self._drawing_image.size(),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        self._square_stroke_mask.fill(Qt.GlobalColor.transparent)
        self._stroke_preview_item.setPixmap(QPixmap())
        self._stroke_preview_item.setOpacity(1.0 if self._drawing_tool == "eraser" else self._brush_opacity)
        self._stroke_preview_item.show()

    def _refresh_stroke_preview_item(self) -> None:
        if self._stroke_preview_item is None or self._stroke_preview_image.isNull():
            return
        if self._drawing_tool == "eraser":
            preview = self._drawing_image.copy()
            painter = QPainter(preview)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
            painter.drawImage(0, 0, self._stroke_preview_image)
            painter.end()
            self._stroke_preview_item.setOpacity(1.0)
            self._stroke_preview_item.setPixmap(QPixmap.fromImage(preview))
            if self._drawing_item is not None:
                self._drawing_item.hide()
        else:
            self._stroke_preview_item.setOpacity(self._brush_opacity)
            self._stroke_preview_item.setPixmap(QPixmap.fromImage(self._stroke_preview_image))
            if self._drawing_item is not None:
                self._drawing_item.show()
        self.viewport().update()

    def _commit_stroke_preview(self) -> tuple[str, QRect, QImage, QImage] | None:
        if self._stroke_preview_image.isNull() or self._drawing_image.isNull():
            return None
        rect = self._stroke_dirty_rect.intersected(self._drawing_image.rect())
        if rect.isEmpty():
            return None
        before_patch = self._drawing_image.copy(rect)
        painter = QPainter(self._drawing_image)
        painter.setClipRect(rect)
        if self._drawing_tool == "eraser":
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
        else:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setOpacity(self._brush_opacity)
        painter.drawImage(0, 0, self._stroke_preview_image)
        painter.end()
        self._refresh_drawing_item()
        return self._drawing_tool, rect, before_patch, self._drawing_image.copy(rect)

    def _clear_stroke_preview_layer(self) -> None:
        self._stroke_preview_image = QImage()
        self._square_stroke_mask = QImage()
        if self._stroke_preview_item is not None:
            self._stroke_preview_item.setPixmap(QPixmap())
            self._stroke_preview_item.hide()
        if self._drawing_item is not None:
            self._drawing_item.show()

    def _stamp_spacing(self) -> float:
        return max(0.25, self._brush_size * 0.08)

    def _rebuild_soft_stroke_preview(self) -> None:
        self._soft_renderer.render_colored(
            self._stroke_preview_image,
            self._brush_color,
            mask_only=self._soft_debug_mode == "mask",
        )

    def _square_tip_mask(self) -> QImage:
        dpr = max(1.0, self.devicePixelRatioF())
        key = (self._brush_size, round(dpr * 100))
        cached = self._square_tip_cache.get(key)
        if cached is not None:
            return cached

        width = max(1, math.ceil(self._brush_size * 1.35 * dpr))
        height = max(1, math.ceil(self._brush_size * 0.35 * dpr))
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.GlobalColor.white)
        painter.drawRect(image.rect())
        painter.end()
        image.setDevicePixelRatio(dpr)
        self._square_tip_cache[key] = image
        return image

    def _append_square_curve_stamps(self, start: QPointF, control: QPointF, end: QPointF) -> None:
        length = math.hypot(end.x() - start.x(), end.y() - start.y())
        steps = max(1, math.ceil(length / self._stamp_spacing()))
        for step in range(steps + 1):
            t = step / steps
            inverse = 1 - t
            point = QPointF(
                inverse * inverse * start.x() + 2 * inverse * t * control.x() + t * t * end.x(),
                inverse * inverse * start.y() + 2 * inverse * t * control.y() + t * t * end.y(),
            )
            tangent_x = 2 * inverse * (control.x() - start.x()) + 2 * t * (end.x() - control.x())
            tangent_y = 2 * inverse * (control.y() - start.y()) + 2 * t * (end.y() - control.y())
            angle = self._square_stamp_angle(math.atan2(tangent_y, tangent_x))
            self._square_stroke_stamps.append((point, angle, 1.0))

    def _append_square_line_stamps(
        self,
        start: QPointF,
        end: QPointF,
        start_pressure: float,
        end_pressure: float,
    ) -> None:
        distance = math.hypot(end.x() - start.x(), end.y() - start.y())
        steps = max(1, math.ceil(distance / self._stamp_spacing()))
        angle = self._square_stamp_angle(math.atan2(end.y() - start.y(), end.x() - start.x()))
        for step in range(steps + 1):
            ratio = step / steps
            pressure = start_pressure + (end_pressure - start_pressure) * ratio
            self._square_stroke_stamps.append(
                (
                    QPointF(
                        start.x() + (end.x() - start.x()) * ratio,
                        start.y() + (end.y() - start.y()) * ratio,
                    ),
                    angle,
                    self._square_scale_for_pressure(pressure),
                )
            )

    def _square_stamp_angle(self, tangent_angle: float) -> float:
        if not self._square_brush_follow_path:
            return self._square_brush_fixed_angle
        if self._last_square_angle is None:
            self._last_square_angle = tangent_angle
            return tangent_angle
        delta = (tangent_angle - self._last_square_angle + math.pi) % (2 * math.pi) - math.pi
        self._last_square_angle += delta * 0.18
        return self._last_square_angle

    def _rebuild_square_stroke_preview(self) -> None:
        if self._square_stroke_mask.isNull():
            return
        self._square_stroke_mask.fill(Qt.GlobalColor.transparent)
        if not self._square_stroke_stamps:
            return

        tip = self._square_tip_mask()
        logical_width = tip.width() / tip.devicePixelRatio()
        logical_height = tip.height() / tip.devicePixelRatio()
        painter = QPainter(self._square_stroke_mask)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Lighten)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        for point, angle, scale in self._square_stroke_stamps:
            painter.save()
            painter.translate(point)
            painter.rotate(math.degrees(angle))
            painter.scale(scale, scale)
            painter.drawImage(QPointF(-logical_width / 2, -logical_height / 2), tip)
            painter.restore()
        painter.end()
        self._colorize_stroke_mask(self._square_stroke_mask)

    def _colorize_stroke_mask(self, mask: QImage) -> None:
        self._stroke_preview_image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(self._stroke_preview_image)
        painter.drawImage(0, 0, mask)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(self._stroke_preview_image.rect(), QColor(self._brush_color))
        painter.end()

    def _refresh_drawing_onion_pixmaps(self) -> None:
        if self._drawing_previous_item is None or self._drawing_next_item is None:
            return
        if not self._drawing_previous_source_pixmap.isNull():
            self._drawing_previous_item.setPixmap(
                self._tint_drawing_pixmap(self._drawing_previous_source_pixmap, self._drawing_previous_onion_color)
            )
        if not self._drawing_next_source_pixmap.isNull():
            self._drawing_next_item.setPixmap(
                self._tint_drawing_pixmap(self._drawing_next_source_pixmap, self._drawing_next_onion_color)
            )
        self._sync_drawing_onion_state()

    def _sync_drawing_onion_state(self) -> None:
        if self._drawing_previous_item is None or self._drawing_next_item is None:
            return
        show_previous = self._drawing_onion_enabled and not self._drawing_previous_item.pixmap().isNull()
        show_next = self._drawing_onion_enabled and not self._drawing_next_item.pixmap().isNull()
        self._drawing_previous_item.setVisible(show_previous)
        self._drawing_next_item.setVisible(show_next)
        self._drawing_previous_item.setOpacity(self._drawing_previous_onion_opacity)
        self._drawing_next_item.setOpacity(self._drawing_next_onion_opacity)
        self.viewport().update()

    @staticmethod
    def _tint_drawing_pixmap(pixmap: QPixmap, color: QColor) -> QPixmap:
        if pixmap.isNull():
            return QPixmap()
        tinted = QPixmap(pixmap.size())
        tinted.fill(Qt.GlobalColor.transparent)
        painter = QPainter(tinted)
        painter.drawPixmap(0, 0, pixmap)
        # Drawing layers often contain black strokes.  Multiply preserves black,
        # so the onion color was effectively invisible.  SourceIn replaces RGB
        # while retaining the original alpha mask.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(tinted.rect(), color)
        painter.end()
        return tinted

    def _flush_drawing_preview(self) -> None:
        if not self._drawing_dirty:
            return
        self._drawing_preview_timer.stop()
        self._drawing_dirty = False
        self._paint_pending_stroke()

    def _cancel_stroke_state(self) -> None:
        self._drawing_preview_timer.stop()
        self._is_drawing = False
        self._gradient_start = None
        self._clear_stroke_preview_layer()
        self._reset_stroke_tracking()

    def _reset_stroke_tracking(self) -> None:
        self._last_draw_point = QPointF()
        self._last_input_point = QPointF()
        self._last_draw_pressure = 1.0
        self._last_input_pressure = 1.0
        self._press_pressure = 1.0
        self._filtered_pressure = 1.0
        self._previous_point = QPointF()
        self._midpoint = QPointF()
        self._control_point = QPointF()
        self._last_move_timestamp = -1
        self._press_point = None
        self._waiting_for_first_motion = False
        self._stroke_points = []
        self._stroke_path = QPainterPath()
        self._stroke_dot_points.clear()
        self._soft_renderer.reset()
        self._square_stroke_stamps.clear()
        self._stroke_dirty_rect = QRect()
        self._pencil_renderer.reset()
        self._last_square_angle = None
        self._stroke_has_content = False
        self._drawing_dirty = False

    def _soft_radius_for_pressure(self, pressure: float) -> float:
        scale = pressure if self._soft_pressure_size else 1.0
        return max(0.5, self._brush_size * scale / 2)

    def _soft_strength_for_pressure(self, pressure: float) -> float:
        return pressure if self._soft_pressure_opacity else 1.0

    def _square_scale_for_pressure(self, pressure: float) -> float:
        if not self._square_pressure_size_enabled:
            return 1.0
        pressure = max(0.0, min(1.0, pressure))
        scale = 1.0 - self._square_pressure_size_strength * (1.0 - pressure)
        return max(self._square_min_pressure_size, min(1.0, scale))

    def _smoothed_event_pressure(self, event: QMouseEvent, *, reset: bool = False) -> float:
        try:
            raw_pressure = float(event.point(0).pressure())
        except (AttributeError, IndexError, TypeError):
            raw_pressure = 1.0
        raw_pressure = raw_pressure if raw_pressure > 0.0 else 1.0
        raw_pressure = max(0.05, min(1.0, raw_pressure))
        if reset:
            self._filtered_pressure = raw_pressure
            return raw_pressure

        # Pressure samples can be sparse and noisy.  Limit the allowed change
        # per event, then apply a low-pass response before arc-length resampling.
        delta = max(-0.18, min(0.18, raw_pressure - self._filtered_pressure))
        self._filtered_pressure = max(0.05, min(1.0, self._filtered_pressure + delta * 0.38))
        return self._filtered_pressure

    def _resample_spacing(self) -> float:
        return min(1.5, max(0.75, self._brush_size * 0.08))

    @staticmethod
    def _midpoint(first: QPointF, second: QPointF) -> QPointF:
        return QPointF((first.x() + second.x()) / 2, (first.y() + second.y()) / 2)

    @staticmethod
    def _points_are_close(first: QPointF, second: QPointF) -> bool:
        return abs(first.x() - second.x()) < 0.01 and abs(first.y() - second.y()) < 0.01

    def _accept_move_event(self, event: QMouseEvent) -> bool:
        timestamp = int(event.timestamp())
        if timestamp and timestamp <= self._last_move_timestamp:
            return False
        if timestamp:
            self._last_move_timestamp = timestamp
        return True

    def _is_abnormal_jump(self, point: QPointF) -> bool:
        if self._waiting_for_first_motion:
            return False
        distance = math.hypot(
            point.x() - self._last_input_point.x(),
            point.y() - self._last_input_point.y(),
        )
        return distance > max(240.0, self._brush_size * 40.0)

    def _scene_point_from_viewport(self, viewport_pos: QPointF) -> QPointF:
        inverted_transform, invertible = self.viewportTransform().inverted()
        if invertible:
            return inverted_transform.map(viewport_pos)
        return self.mapToScene(viewport_pos.toPoint())

    def _update_drawing_cursor(self, viewport_pos: QPointF) -> None:
        if not self._drawing_enabled:
            return

        scene_pos = self._scene_point_from_viewport(viewport_pos)
        in_bounds = self._drawing_bounds_rect().contains(scene_pos)
        if self._cursor_in_bounds == in_bounds and self._cursor_scene_pos == scene_pos:
            return

        self._cursor_scene_pos = scene_pos if in_bounds else None
        self._cursor_in_bounds = in_bounds
        if in_bounds:
            self.viewport().setCursor(Qt.CursorShape.BlankCursor)
        else:
            self.viewport().unsetCursor()
        self.viewport().update()


class CanvasView(DrawingGraphicsView):
    """Zoomable canvas with optional onion-skin frame layers."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))

        self._previous_item = QGraphicsPixmapItem()
        self._next_item = QGraphicsPixmapItem()
        self._current_item = QGraphicsPixmapItem()
        self._tracing_item = QGraphicsRectItem(0, 0, 1024, 768)
        self._previous_source_pixmap = QPixmap()
        self._current_source_pixmap = QPixmap()
        self._next_source_pixmap = QPixmap()

        self._previous_item.setZValue(-20)
        self._next_item.setZValue(-10)
        self._current_item.setZValue(0)
        self._tracing_item.setZValue(20)
        self._tracing_item.setBrush(QColor(255, 255, 255, 18))
        self._tracing_item.setPen(QPen(QColor("#60a5fa"), 1))

        self.scene().addItem(self._previous_item)
        self.scene().addItem(self._next_item)
        self.scene().addItem(self._current_item)
        self.scene().addItem(self._tracing_item)
        self._init_drawing_layer(30)

        self._onion_skin_enabled = False
        self._previous_onion_opacity = 0.3
        self._next_onion_opacity = 0.2
        self._previous_onion_color = QColor("#ef4444")
        self._next_onion_color = QColor("#3b82f6")
        self._global_opacity = 1.0
        self._tracing_visible = True
        self._grid_enabled = False
        self._grid_size = 50
        self._grid_color = QColor("#2563eb")
        self._grid_opacity = 0.4
        self._content_scale = 1

        self.setBackgroundBrush(QColor("#111827"))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setRenderHints(self.renderHints() | QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._sync_onion_skin_state()

    def set_pixmap(self, pixmap: QPixmap) -> None:
        """Compatibility helper for callers that only need one visible frame."""

        self.set_frame_layers(None, pixmap, None)

    def set_frame_layers(
        self,
        previous_pixmap: QPixmap | None,
        current_pixmap: QPixmap,
        next_pixmap: QPixmap | None,
    ) -> None:
        """Set the previous/current/next frame layers.

        Previous and next frames are translucent reference layers below the
        current frame.
        """

        should_fit = self._current_item.pixmap().isNull()
        self._previous_source_pixmap = previous_pixmap or QPixmap()
        self._current_source_pixmap = current_pixmap
        self._next_source_pixmap = next_pixmap or QPixmap()
        self._refresh_display_pixmaps()
        if should_fit:
            self.fit_to_view()

    def set_content_scale(self, scale: int) -> None:
        if scale < 1:
            return
        self._content_scale = scale
        self._refresh_display_pixmaps()

    def set_onion_skin_enabled(self, enabled: bool) -> None:
        self._onion_skin_enabled = enabled
        self._sync_onion_skin_state()

    def set_onion_skin_opacity(self, opacity: float) -> None:
        self._previous_onion_opacity = max(0.0, min(0.3, opacity))
        self._next_onion_opacity = max(0.0, min(0.22, opacity * 0.72))
        self._sync_onion_skin_state()

    def set_frame_opacity(self, opacity: float) -> None:
        self._global_opacity = max(0.0, min(1.0, opacity))
        self._sync_onion_skin_state()

    def set_tracing_visible(self, visible: bool) -> None:
        self._tracing_visible = visible
        self._sync_onion_skin_state()

    def set_grid_enabled(self, enabled: bool) -> None:
        self._grid_enabled = enabled
        self.viewport().update()

    def set_grid_size(self, size: int) -> None:
        if size not in (25, 50, 100):
            return
        self._grid_size = size
        self.viewport().update()

    def set_grid_color(self, color: QColor | str) -> None:
        self._grid_color = QColor(color)
        self.viewport().update()

    def set_grid_opacity(self, opacity: float) -> None:
        self._grid_opacity = max(0.0, min(1.0, opacity))
        self.viewport().update()

    def set_previous_onion_skin_color(self, color: QColor | str) -> None:
        self._previous_onion_color = QColor(color)
        self._refresh_display_pixmaps()

    def set_next_onion_skin_color(self, color: QColor | str) -> None:
        self._next_onion_color = QColor(color)
        self._refresh_display_pixmaps()

    def fit_to_view(self) -> None:
        if self._current_item.pixmap().isNull():
            return

        self.resetTransform()
        self.fitInView(self._current_item, Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        if self._current_item.pixmap().isNull():
            return

        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # type: ignore[override]
        super().drawForeground(painter, rect)
        if self._grid_enabled:
            self._draw_grid(painter, rect)

    def _sync_onion_skin_state(self) -> None:
        show_previous = self._onion_skin_enabled and not self._previous_item.pixmap().isNull()
        show_next = self._onion_skin_enabled and not self._next_item.pixmap().isNull()

        self._previous_item.setVisible(show_previous)
        self._next_item.setVisible(show_next)
        self._previous_item.setOpacity(self._previous_onion_opacity * self._global_opacity)
        self._next_item.setOpacity(self._next_onion_opacity * self._global_opacity)
        self._current_item.setOpacity(self._global_opacity)
        self._tracing_item.setVisible(self._tracing_visible)

    def _tint_pixmap_multiply(self, pixmap: QPixmap, color: QColor) -> QPixmap:
        if pixmap.isNull():
            return QPixmap()

        tinted = QPixmap(pixmap.size())
        tinted.fill(Qt.GlobalColor.transparent)

        painter = QPainter(tinted)
        painter.drawPixmap(0, 0, pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
        painter.fillRect(tinted.rect(), color)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()

        return tinted

    def _draw_grid(self, painter: QPainter, rect: QRectF) -> None:
        if self._current_item.pixmap().isNull():
            return
        _draw_scene_grid(
            painter,
            rect,
            self._current_item.sceneBoundingRect(),
            self._effective_grid_size(),
            self._grid_color,
            self._grid_opacity,
        )

    def _refresh_display_pixmaps(self) -> None:
        previous_pixmap = self._scaled_pixmap(self._previous_source_pixmap)
        current_pixmap = self._scaled_pixmap(self._current_source_pixmap)
        next_pixmap = self._scaled_pixmap(self._next_source_pixmap)

        self._previous_item.setPixmap(self._tint_pixmap_multiply(previous_pixmap, self._previous_onion_color))
        self._current_item.setPixmap(current_pixmap)
        self._next_item.setPixmap(self._tint_pixmap_multiply(next_pixmap, self._next_onion_color))
        self._tracing_item.setRect(self._current_item.boundingRect())
        self.set_drawing_layer_size(current_pixmap.width(), current_pixmap.height())

        scene_rect = self._current_item.boundingRect()
        if not self._previous_item.pixmap().isNull():
            scene_rect = scene_rect.united(self._previous_item.boundingRect())
        if not self._next_item.pixmap().isNull():
            scene_rect = scene_rect.united(self._next_item.boundingRect())
        self.scene().setSceneRect(scene_rect)

        self._sync_onion_skin_state()
        self.viewport().update()

    def _scaled_pixmap(self, pixmap: QPixmap) -> QPixmap:
        if pixmap.isNull() or self._content_scale == 1:
            return pixmap

        scaled_size = QSize(
            pixmap.width() * self._content_scale,
            pixmap.height() * self._content_scale,
        )
        return pixmap.scaled(
            scaled_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _effective_grid_size(self) -> int:
        return max(1, self._grid_size * self._content_scale)


class BlankCanvasView(DrawingGraphicsView):
    """A simple blank canvas area that can be zoomed and panned."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self._canvas_item = QGraphicsRectItem(0, 0, 1024, 768)
        self._canvas_item.setBrush(QColor("#ffffff"))
        self._canvas_item.setPen(QPen(QColor("#d1d5db"), 1))
        self.scene().addItem(self._canvas_item)
        self._init_drawing_layer(10)
        self.scene().setSceneRect(self._canvas_item.boundingRect())

        self.setBackgroundBrush(QColor("#1f2937"))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setRenderHints(self.renderHints() | QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._grid_enabled = False
        self._grid_size = 50
        self._grid_color = QColor("#2563eb")
        self._grid_opacity = 0.4
        self._grid_scale = 1
        self.set_drawing_layer_size(1024, 768)
        self.fit_to_view()

    def set_canvas_size(self, width: int, height: int, grid_scale: int = 1) -> None:
        self._grid_scale = max(1, grid_scale)
        self._canvas_item.setRect(0, 0, max(1, width), max(1, height))
        self.set_drawing_layer_size(max(1, width), max(1, height))
        self.scene().setSceneRect(self._canvas_item.boundingRect())
        self.viewport().update()

    def set_grid_enabled(self, enabled: bool) -> None:
        self._grid_enabled = enabled
        self.viewport().update()

    def set_grid_size(self, size: int) -> None:
        if size not in (25, 50, 100):
            return
        self._grid_size = size
        self.viewport().update()

    def set_grid_color(self, color: QColor | str) -> None:
        self._grid_color = QColor(color)
        self.viewport().update()

    def set_grid_opacity(self, opacity: float) -> None:
        self._grid_opacity = max(0.0, min(1.0, opacity))
        self.viewport().update()

    def fit_to_view(self) -> None:
        self.resetTransform()
        self.fitInView(self._canvas_item, Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # type: ignore[override]
        super().drawForeground(painter, rect)
        if self._grid_enabled:
            self._draw_grid(painter, rect)

    def _draw_grid(self, painter: QPainter, rect: QRectF) -> None:
        _draw_scene_grid(
            painter,
            rect,
            self._canvas_item.sceneBoundingRect(),
            self._effective_grid_size(),
            self._grid_color,
            self._grid_opacity,
        )

    def _effective_grid_size(self) -> int:
        return max(1, self._grid_size * self._grid_scale)
