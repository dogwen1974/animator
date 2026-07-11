from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap
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


class DrawingGraphicsView(QGraphicsView):
    drawing_changed = Signal(QImage)
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
        self._soft_stroke_mask = QImage()
        self._square_stroke_mask = QImage()
        self._drawing_enabled = False
        self._drawing_blocked = False
        self._drawing_tool = "brush"
        self._brush_color = QColor("#000000")
        self._brush_opacity = 1.0
        self._brush_hardness = 45
        self._brush_size = 4
        self._soft_tip_cache: dict[tuple[int, int, int], QImage] = {}
        self._square_tip_cache: dict[tuple[int, int], QImage] = {}
        self._square_brush_follow_path = False
        self._square_brush_fixed_angle = math.radians(45)
        self._last_square_angle: float | None = None
        self._is_drawing = False
        self._last_draw_point = QPointF()
        self._last_input_point = QPointF()
        self._previous_point = QPointF()
        self._midpoint = QPointF()
        self._control_point = QPointF()
        self._last_move_timestamp = -1
        self._press_point: QPointF | None = None
        self._waiting_for_first_motion = False
        self._stroke_points: list[QPointF] = []
        self._stroke_path = QPainterPath()
        self._stroke_dot_points: list[QPointF] = []
        self._soft_stroke_points: list[QPointF] = []
        self._square_stroke_stamps: list[tuple[QPointF, float]] = []
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
        if tool in ("brush", "soft_brush", "square_brush", "eraser"):
            self._finish_active_stroke(emit_change=True)
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

    def clear_drawing(self) -> None:
        if self._drawing_image.isNull():
            return
        self._cancel_stroke_state()
        self._drawing_image.fill(Qt.GlobalColor.transparent)
        self._refresh_drawing_item()
        self.drawing_changed.emit(self._drawing_image.copy())

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
                self._begin_stroke(self._clamped_point(scene_pos))
                self._is_drawing = True
                self.stroke_started.emit(self._drawing_image.copy())
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        self._update_drawing_cursor(event.position())
        if self._is_drawing:
            if not self._accept_move_event(event):
                event.accept()
                return
            scene_pos = self._scene_point_from_viewport(event.position())
            if self._is_abnormal_jump(scene_pos):
                event.accept()
                return
            self._draw_stroke(self._last_draw_point, scene_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._is_drawing and event.button() == Qt.MouseButton.LeftButton:
            self._append_input_point(
                self._clamped_point(self._scene_point_from_viewport(event.position())),
                force=True,
            )
            self._finish_active_stroke(emit_change=True)
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

    def _draw_stroke(self, start: QPointF, end: QPointF) -> None:
        if self._drawing_image.isNull():
            return

        start = self._clamped_point(start)
        end = self._clamped_point(end)
        if self._press_point is None:
            self._begin_stroke(start)
        self._append_input_point(end)

    def _begin_stroke(self, point: QPointF) -> None:
        self._cancel_stroke_state()
        self._create_stroke_preview_layer()
        self._last_draw_point = QPointF()
        self._last_input_point = point
        self._stroke_points = []
        self._stroke_path = QPainterPath()
        self._stroke_dot_points.clear()
        self._soft_stroke_points.clear()
        self._square_stroke_stamps.clear()
        self._last_square_angle = None
        self._press_point = point
        self._waiting_for_first_motion = True
        self._stroke_has_content = False

    def _append_input_point(self, point: QPointF, *, force: bool = False) -> None:
        point = self._clamped_point(point)
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
            self._queue_linear_segment(start, point, path_already_started=True)
            return

        if not force and distance < self._resample_spacing() * 0.35:
            return

        steps = max(1, math.ceil(distance / self._resample_spacing()))
        origin = self._last_input_point
        for step in range(1, steps + 1):
            ratio = step / steps
            sample = QPointF(
                origin.x() + (point.x() - origin.x()) * ratio,
                origin.y() + (point.y() - origin.y()) * ratio,
            )
            self._append_smoothed_point(sample)
        self._last_input_point = point

    def _append_smoothed_point(self, point: QPointF) -> None:
        if self._stroke_points and self._points_are_close(self._stroke_points[-1], point):
            return

        self._queue_linear_segment(self._last_draw_point, point)
        self._previous_point = self._last_draw_point
        self._last_draw_point = point
        self._stroke_points = [point]

    def _queue_linear_segment(self, start: QPointF, end: QPointF, *, path_already_started: bool = False) -> None:
        if self._points_are_close(start, end):
            return
        if self._stroke_path.isEmpty():
            self._stroke_path.moveTo(start)
        if not path_already_started:
            self._stroke_path.lineTo(end)
        if self._drawing_tool == "soft_brush":
            self._append_soft_line_points(start, end)
        elif self._drawing_tool == "square_brush":
            self._append_square_line_stamps(start, end)
        self._stroke_has_content = True
        self._schedule_drawing_preview()

    def _queue_quadratic_segment(self, start: QPointF, control: QPointF, end: QPointF) -> None:
        if self._points_are_close(start, end):
            return
        if self._stroke_path.isEmpty():
            self._stroke_path.moveTo(start)
        self._stroke_path.quadTo(control, end)
        if self._drawing_tool == "soft_brush":
            self._append_soft_curve_points(start, control, end)
        elif self._drawing_tool == "square_brush":
            self._append_square_curve_stamps(start, control, end)
        self._stroke_has_content = True
        self._schedule_drawing_preview()

    def _queue_dot(self, point: QPointF) -> None:
        self._stroke_dot_points = [point]
        if self._drawing_tool == "soft_brush":
            self._soft_stroke_points = [point]
        elif self._drawing_tool == "square_brush":
            self._square_stroke_stamps = [(point, self._square_brush_fixed_angle)]
        self._stroke_has_content = True
        self._schedule_drawing_preview()

    def _schedule_drawing_preview(self) -> None:
        self._drawing_dirty = True
        if not self._drawing_preview_timer.isActive():
            self._drawing_preview_timer.start(16)

    def _finish_active_stroke(self, *, emit_change: bool) -> None:
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

        self._flush_drawing_preview()
        changed = self._stroke_has_content
        if changed:
            self._commit_stroke_preview()
        self._clear_stroke_preview_layer()
        self._reset_stroke_tracking()
        if changed and emit_change:
            self.drawing_changed.emit(self._drawing_image.copy())

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
        self._soft_stroke_mask = QImage(
            self._drawing_image.size(),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        self._soft_stroke_mask.fill(Qt.GlobalColor.transparent)
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
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
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

    def _commit_stroke_preview(self) -> None:
        if self._stroke_preview_image.isNull() or self._drawing_image.isNull():
            return
        painter = QPainter(self._drawing_image)
        if self._drawing_tool == "eraser":
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        else:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setOpacity(self._brush_opacity)
        painter.drawImage(0, 0, self._stroke_preview_image)
        painter.end()
        self._refresh_drawing_item()

    def _clear_stroke_preview_layer(self) -> None:
        self._stroke_preview_image = QImage()
        self._soft_stroke_mask = QImage()
        self._square_stroke_mask = QImage()
        if self._stroke_preview_item is not None:
            self._stroke_preview_item.setPixmap(QPixmap())
            self._stroke_preview_item.hide()
        if self._drawing_item is not None:
            self._drawing_item.show()

    def _append_soft_curve_points(self, start: QPointF, control: QPointF, end: QPointF) -> None:
        length = math.hypot(end.x() - start.x(), end.y() - start.y())
        steps = max(1, math.ceil(length / self._soft_stamp_spacing()))
        for step in range(steps + 1):
            t = step / steps
            inverse = 1 - t
            self._soft_stroke_points.append(
                QPointF(
                    inverse * inverse * start.x() + 2 * inverse * t * control.x() + t * t * end.x(),
                    inverse * inverse * start.y() + 2 * inverse * t * control.y() + t * t * end.y(),
                )
            )

    def _append_soft_line_points(self, start: QPointF, end: QPointF) -> None:
        distance = math.hypot(end.x() - start.x(), end.y() - start.y())
        steps = max(1, math.ceil(distance / self._soft_stamp_spacing()))
        for step in range(steps + 1):
            ratio = step / steps
            self._soft_stroke_points.append(
                QPointF(
                    start.x() + (end.x() - start.x()) * ratio,
                    start.y() + (end.y() - start.y()) * ratio,
                )
            )

    def _soft_stamp_spacing(self) -> float:
        return max(0.25, self._brush_size * 0.08)

    def _soft_tip_mask(self) -> QImage:
        dpr = max(1.0, self.devicePixelRatioF())
        key = (self._brush_size, self._brush_hardness, round(dpr * 100))
        cached = self._soft_tip_cache.get(key)
        if cached is not None:
            return cached

        diameter = max(1, math.ceil(self._brush_size * dpr))
        image = QImage(diameter, diameter, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        radius = diameter / 2
        hard_radius = radius * (self._brush_hardness / 100)
        for y in range(diameter):
            for x in range(diameter):
                distance = math.hypot(x + 0.5 - radius, y + 0.5 - radius)
                if distance >= radius:
                    alpha = 0
                elif self._brush_hardness >= 100 or distance <= hard_radius:
                    alpha = 255
                else:
                    ratio = (distance - hard_radius) / max(0.001, radius - hard_radius)
                    smoothstep = ratio * ratio * (3 - 2 * ratio)
                    alpha = round(255 * (1 - smoothstep))
                image.setPixelColor(x, y, QColor(255, 255, 255, alpha))
        image.setDevicePixelRatio(dpr)
        self._soft_tip_cache[key] = image
        return image

    def _rebuild_soft_stroke_preview(self) -> None:
        if self._soft_stroke_mask.isNull():
            return
        self._soft_stroke_mask.fill(Qt.GlobalColor.transparent)
        if not self._soft_stroke_points:
            return

        tip = self._soft_tip_mask()
        painter = QPainter(self._soft_stroke_mask)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Lighten)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        offset_x = tip.width() / (2 * tip.devicePixelRatio())
        offset_y = tip.height() / (2 * tip.devicePixelRatio())
        for point in self._soft_stroke_points:
            painter.drawImage(QPointF(point.x() - offset_x, point.y() - offset_y), tip)
        painter.end()
        self._colorize_stroke_mask(self._soft_stroke_mask)

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
        steps = max(1, math.ceil(length / self._soft_stamp_spacing()))
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
            self._square_stroke_stamps.append((point, angle))

    def _append_square_line_stamps(self, start: QPointF, end: QPointF) -> None:
        distance = math.hypot(end.x() - start.x(), end.y() - start.y())
        steps = max(1, math.ceil(distance / self._soft_stamp_spacing()))
        angle = self._square_stamp_angle(math.atan2(end.y() - start.y(), end.x() - start.x()))
        for step in range(steps + 1):
            ratio = step / steps
            self._square_stroke_stamps.append(
                (
                    QPointF(
                        start.x() + (end.x() - start.x()) * ratio,
                        start.y() + (end.y() - start.y()) * ratio,
                    ),
                    angle,
                )
            )

    def _square_stamp_angle(self, tangent_angle: float) -> float:
        if not self._square_brush_follow_path:
            return self._square_brush_fixed_angle
        if self._last_square_angle is None:
            self._last_square_angle = tangent_angle
            return tangent_angle
        delta = (tangent_angle - self._last_square_angle + math.pi) % (2 * math.pi) - math.pi
        self._last_square_angle += delta * 0.35
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
        for point, angle in self._square_stroke_stamps:
            painter.save()
            painter.translate(point)
            painter.rotate(math.degrees(angle))
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
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
        painter.fillRect(tinted.rect(), color)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        return tinted

    def _flush_drawing_preview(self) -> None:
        if not self._drawing_dirty:
            return
        self._drawing_preview_timer.stop()
        self._drawing_dirty = False
        self._paint_pending_stroke()
        self._refresh_drawing_item()

    def _cancel_stroke_state(self) -> None:
        self._drawing_preview_timer.stop()
        self._is_drawing = False
        self._clear_stroke_preview_layer()
        self._reset_stroke_tracking()

    def _reset_stroke_tracking(self) -> None:
        self._last_draw_point = QPointF()
        self._last_input_point = QPointF()
        self._previous_point = QPointF()
        self._midpoint = QPointF()
        self._control_point = QPointF()
        self._last_move_timestamp = -1
        self._press_point = None
        self._waiting_for_first_motion = False
        self._stroke_points = []
        self._stroke_path = QPainterPath()
        self._stroke_dot_points.clear()
        self._soft_stroke_points.clear()
        self._square_stroke_stamps.clear()
        self._last_square_angle = None
        self._stroke_has_content = False
        self._drawing_dirty = False

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
