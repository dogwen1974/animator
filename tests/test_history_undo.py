from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QAbstractItemView, QListWidget

from history import HistoryMemoryManager
from pyside6_frame_viewer import FrameViewerWindow


class DrawingHistoryUndoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.window = FrameViewerWindow()
        self.window.current_index = 0
        self.window.current_drawing_size = QSize(64, 64)
        self.window.drawing_layers[0] = self._blank_image()
        self.window.frame_histories.clear()
        self.window._history_memory.reset()

    def tearDown(self) -> None:
        self.window.close()

    @staticmethod
    def _blank_image() -> QImage:
        image = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0, 0))
        return image

    @staticmethod
    def _fill_rect(image: QImage, rect: QRect, color: QColor) -> QImage:
        result = image.copy()
        painter = QPainter(result)
        painter.fillRect(rect, color)
        painter.end()
        return result

    @staticmethod
    def _contains_color(image: QImage, color: QColor) -> bool:
        for y in range(image.height()):
            for x in range(image.width()):
                pixel = image.pixelColor(x, y)
                if pixel.alpha() and abs(pixel.red() - color.red()) < 32 and abs(pixel.green() - color.green()) < 32 and abs(pixel.blue() - color.blue()) < 32:
                    return True
        return False

    def _commit_image_change(self, kind: str, before: QImage, after: QImage) -> None:
        self.window._pending_stroke_before[self.window.frames[0].frame_id] = (kind, before)
        self.window.update_current_drawing_layer(after)

    def _assert_round_trip(self, before: QImage, after: QImage, kind: str, rect: QRect) -> None:
        self._commit_image_change(kind, before, after)
        operation = self.window.frame_histories[0].undo[-1]
        self.assertEqual(operation.kind, kind)
        self.assertEqual(operation.rect, rect)
        self.assertEqual(operation.before_patch.size(), rect.size())
        self.assertEqual(operation.after_patch.size(), rect.size())

        self.window.undo_current_frame_drawing()
        self.assertEqual(self.window.drawing_layers[0], before)
        self.window.redo_current_frame_drawing()
        self.assertEqual(self.window.drawing_layers[0], after)

    def test_normal_brush_stroke_undo_redo_uses_changed_rect(self) -> None:
        before = self._blank_image()
        rect = QRect(10, 14, 16, 5)
        after = self._fill_rect(before, rect, QColor("#111111"))
        self._assert_round_trip(before, after, "brush", rect)

    def test_clear_undo_redo_uses_only_painted_rect(self) -> None:
        rect = QRect(8, 9, 12, 10)
        before = self._fill_rect(self._blank_image(), rect, QColor("#2563eb"))
        self.window.drawing_layers[0] = before.copy()
        self.window.clear_current_drawing_layer()

        operation = self.window.frame_histories[0].undo[-1]
        self.assertEqual(operation.kind, "clear")
        self.assertEqual(operation.rect, rect)
        self.window.undo_current_frame_drawing()
        self.assertEqual(self.window.drawing_layers[0], before)
        self.window.redo_current_frame_drawing()
        self.assertTrue(self.window.drawing_layers[0].pixelColor(10, 10).alpha() == 0)

    def test_bucket_undo_redo_uses_flooded_area_rect(self) -> None:
        before = self._blank_image()
        rect = QRect(4, 6, 20, 17)
        after = self._fill_rect(before, rect, QColor("#ef4444"))
        self._assert_round_trip(before, after, "bucket", rect)

    def test_gradient_undo_redo_uses_canvas_change_rect(self) -> None:
        before = self._blank_image()
        after = before.copy()
        painter = QPainter(after)
        for x in range(after.width()):
            color = QColor(x * 4, 20, 255 - x * 4, 255)
            painter.setPen(color)
            painter.drawLine(x, 0, x, after.height() - 1)
        painter.end()
        self._assert_round_trip(before, after, "gradient", after.rect())

    def test_ctrl_drag_moves_drawing_and_is_undoable(self) -> None:
        before = self._blank_image()
        before.setPixelColor(10, 12, QColor("#2563eb"))
        self.window.drawing_layers[0] = before.copy()
        self.window.frame_histories.clear()
        self.window._history_memory.reset()
        canvas = self.window.overlay_canvas
        reference = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
        reference.fill(QColor("#ef4444"))
        canvas.set_frame_layers(None, QPixmap.fromImage(reference), None)
        reference_key = canvas._current_item.pixmap().cacheKey()
        reference_position = canvas._current_item.pos()
        canvas.set_drawing_image(before)

        class CtrlLeftEvent:
            def __init__(self, position) -> None:
                self._position = QPointF(position)
                self.accepted = False

            def position(self):
                return self._position

            @staticmethod
            def button():
                return Qt.MouseButton.LeftButton

            @staticmethod
            def modifiers():
                return Qt.KeyboardModifier.ControlModifier

            def accept(self) -> None:
                self.accepted = True

        press = CtrlLeftEvent(canvas.mapFromScene(QPointF(4, 5)))
        move = CtrlLeftEvent(canvas.mapFromScene(QPointF(10, 9)))
        release = CtrlLeftEvent(canvas.mapFromScene(QPointF(10, 9)))
        canvas.mousePressEvent(press)
        self.assertIsNone(canvas._cursor_scene_pos)
        self.assertEqual(canvas.viewport().cursor().shape(), Qt.CursorShape.ClosedHandCursor)
        canvas.mouseMoveEvent(move)
        delta_x = round(canvas._translation_delta.x())
        delta_y = round(canvas._translation_delta.y())
        canvas.mouseReleaseEvent(release)
        self.assertTrue(press.accepted and move.accepted and release.accepted)

        moved = self.window.drawing_layers[0]
        self.assertNotEqual((delta_x, delta_y), (0, 0))
        self.assertEqual(moved.pixelColor(10 + delta_x, 12 + delta_y), QColor("#2563eb"))
        self.assertEqual(moved.pixelColor(10, 12).alpha(), 0)
        self.assertEqual(canvas._current_item.pixmap().cacheKey(), reference_key)
        self.assertEqual(canvas._current_item.pos(), reference_position)
        self.assertEqual(self.window.frame_histories[0].undo[-1].kind, "move")
        self.window.undo_current_frame_drawing()
        self.assertEqual(self.window.drawing_layers[0], before)
        self.window.redo_current_frame_drawing()
        self.assertEqual(self.window.drawing_layers[0], moved)

    def test_brush_path_uses_continuous_quadratic_segments(self) -> None:
        canvas = self.window.trace_only_canvas
        canvas.set_drawing_image(self._blank_image())
        canvas._begin_stroke(QPointF(5, 8))
        canvas._append_input_point(QPointF(14, 10))
        canvas._append_input_point(QPointF(24, 20))
        canvas._append_input_point(QPointF(36, 16))

        element_types = [canvas._stroke_path.elementAt(index).type for index in range(canvas._stroke_path.elementCount())]
        self.assertIn(QPainterPath.ElementType.CurveToElement, element_types)
        self.assertGreater(len(canvas._stroke_points), 2)
        canvas._is_drawing = True
        canvas._finish_active_stroke(emit_change=False)

    def test_global_memory_limit_evicts_oldest_operation_across_frames(self) -> None:
        self.window._history_memory = HistoryMemoryManager(limit_bytes=8)
        first_before = self._blank_image()
        first_after = self._fill_rect(first_before, QRect(1, 1, 1, 1), QColor("black"))
        self._commit_image_change("brush", first_before, first_after)

        self.window.current_index = 1
        self.window.drawing_layers[1] = self._blank_image()
        second_before = self._blank_image()
        second_after = self._fill_rect(second_before, QRect(2, 2, 1, 1), QColor("black"))
        self.window._pending_stroke_before[self.window.frames[1].frame_id] = ("brush", second_before)
        self.window.update_current_drawing_layer(second_after)

        self.assertFalse(self.window.frame_histories[0].undo)
        self.assertEqual(len(self.window.frame_histories[1].undo), 1)
        self.assertLessEqual(self.window._history_memory.total_bytes, 8)

    def _prepare_structural_frames(self) -> None:
        self.window.frame_paths = [Path("frame_a.png"), Path("frame_b.png"), Path("frame_c.png")]
        self.window.frame_durations = [100, 120, 140]
        self.window.frame_exposures = [1, 2, 3]
        self.window._project_reference_images = {}
        self.window.drawing_layers.clear()
        self.window.frame_histories.clear()
        self.window._history_memory.reset()
        for index, color in enumerate((QColor("#ef4444"), QColor("#16a34a"), QColor("#2563eb"))):
            reference = QImage(32, 18, QImage.Format.Format_ARGB32_Premultiplied)
            reference.fill(color)
            self.window._project_reference_images[index] = reference
            drawing = self._blank_image()
            drawing.setPixelColor(index + 2, index + 2, color)
            self.window.drawing_layers[index] = drawing
            before = self._blank_image()
            after = drawing.copy()
            self.window.current_index = index
            self.window._pending_stroke_before[self.window.frames[index].frame_id] = ("brush", before)
            self.window.update_current_drawing_layer(after)
            self.window.drawing_layers[index] = drawing.copy()
        self.window.current_index = 0
        self.window._load_timeline()

    def test_reorder_keeps_frame_state_together(self) -> None:
        self._prepare_structural_frames()
        original_history = self.window.frame_histories[1]
        self.window._reorder_frames([2, 0, 1])
        self.assertEqual(self.window.frame_exposures, [3, 1, 2])
        self.assertEqual(self.window._project_reference_images[0].pixelColor(0, 0), QColor("#2563eb"))
        self.assertTrue(self._contains_color(self.window.drawing_layers[0], QColor("#2563eb")))
        self.assertIs(self.window.frame_histories[2], original_history)

    def test_timeline_drop_preserves_selected_frame_and_all_frame_data(self) -> None:
        self._prepare_structural_frames()
        selected_history = self.window.frame_histories[1]
        self.window.current_index = 1
        frame_ids = [frame.frame_id for frame in self.window.frames]
        self.assertTrue(self.window._move_frame_by_id(frame_ids[1], frame_ids[2], True))

        self.assertTrue(self.window.timeline.dragEnabled())
        self.assertTrue(self.window.timeline.acceptDrops())
        self.assertEqual(self.window.frame_exposures, [1, 3, 2])
        self.assertEqual(self.window.current_index, 2)
        self.assertEqual(self.window._project_reference_images[2].pixelColor(0, 0), QColor("#16a34a"))
        self.assertTrue(self._contains_color(self.window.drawing_layers[2], QColor("#16a34a")))
        self.assertIs(self.window.frame_histories[2], selected_history)

    def _frame_state_by_id(self) -> dict[str, tuple[QColor, Path, object | None]]:
        state: dict[str, tuple[QColor, Path, object | None]] = {}
        for frame in self.window.frames:
            reference = frame.reference_image or QImage()
            state[frame.frame_id] = (
                reference.pixelColor(0, 0),
                frame.path,
                frame.history,
            )
        return state

    def _assert_timeline_matches_frame_ids(self, expected_state: dict[str, tuple[QColor, Path, object | None]]) -> None:
        frame_ids = [frame.frame_id for frame in self.window.frames]
        item_ids = [str(self.window.timeline.item(index).data(0x0100)) for index in range(self.window.timeline.count())]
        self.assertEqual(len(frame_ids), len(expected_state))
        self.assertEqual(item_ids, frame_ids)
        self.assertEqual(set(frame_ids), set(expected_state))
        for index, frame in enumerate(self.window.frames):
            reference_color, path, history = expected_state[frame.frame_id]
            self.assertEqual(frame.reference_image.pixelColor(0, 0), reference_color)
            self.assertEqual(frame.path, path)
            self.assertIsNotNone(frame.drawing)
            self.assertFalse(frame.drawing.isNull())
            self.assertIs(frame.history, history)
            item = self.window.timeline.item(index)
            self.assertEqual(item.sizeHint(), self.window.timeline.gridSize())
            self.assertFalse(item.icon().isNull())

    def test_repeated_frame_id_moves_preserve_all_content_and_selection(self) -> None:
        self._prepare_structural_frames()
        expected_state = self._frame_state_by_id()
        source_id = self.window.frames[1].frame_id
        first_id = self.window.frames[0].frame_id
        last_id = self.window.frames[-1].frame_id

        for _ in range(12):
            self.assertTrue(self.window._move_frame_by_id(source_id, first_id, False))
            self._assert_timeline_matches_frame_ids(expected_state)
            self.assertEqual(self.window.frames[self.window.current_index].frame_id, source_id)
            self.assertTrue(self.window._move_frame_by_id(source_id, last_id, True))
            self._assert_timeline_matches_frame_ids(expected_state)
            self.assertEqual(self.window.frames[self.window.current_index].frame_id, source_id)

    def test_first_last_and_empty_gap_moves_are_atomic(self) -> None:
        self._prepare_structural_frames()
        expected_state = self._frame_state_by_id()
        first_id = self.window.frames[0].frame_id
        last_id = self.window.frames[-1].frame_id

        self.assertTrue(self.window._move_frame_by_id(last_id, first_id, False))
        self.assertEqual(self.window.frames[0].frame_id, last_id)
        self._assert_timeline_matches_frame_ids(expected_state)
        self.assertTrue(self.window._move_frame_by_id(last_id, "", True))
        self.assertEqual(self.window.frames[-1].frame_id, last_id)
        self._assert_timeline_matches_frame_ids(expected_state)

    def test_rapid_moves_and_thumbnail_mode_switch_do_not_cross_write_items(self) -> None:
        self._prepare_structural_frames()
        expected_state = self._frame_state_by_id()
        stale_generation = self.window._thumbnail_generation
        source_id = self.window.frames[0].frame_id

        for step in range(20):
            anchor_id = self.window.frames[-1].frame_id if step % 2 == 0 else self.window.frames[0].frame_id
            self.window._move_frame_by_id(source_id, anchor_id, step % 3 == 0)
            self.window.thumbnail_display_combo.setCurrentIndex(step % 3)
            self.window._apply_pending_thumbnail_display_mode()
            self._assert_timeline_matches_frame_ids(expected_state)

        item = self.window._timeline_item_for_frame_id(source_id)
        self.assertIsNotNone(item)
        before_key = item.icon().cacheKey()
        self.window._update_timeline_item_thumbnail(source_id, stale_generation)
        self.assertEqual(item.icon().cacheKey(), before_key)
        self.assertNotEqual(self.window.timeline.dragDropMode(), QAbstractItemView.DragDropMode.InternalMove)

    def test_real_custom_drag_path_never_invokes_qlistwidget_move_or_removes_source(self) -> None:
        self._prepare_structural_frames()
        self.window.show()
        self.app.processEvents()
        timeline = self.window.timeline
        timeline.setCurrentRow(0)
        source_id = self.window.frames[0].frame_id
        original_ids = [frame.frame_id for frame in self.window.frames]
        original_records = {frame.frame_id: frame for frame in self.window.frames}
        original_icon_keys = {
            str(timeline.item(index).data(0x0100)): timeline.item(index).icon().cacheKey()
            for index in range(timeline.count())
        }
        counts_during_drop: list[int] = []

        class FakeDropEvent:
            def __init__(self, source, mime_data, position) -> None:
                self._source = source
                self._mime_data = mime_data
                self._position = QPointF(position)
                self.accepted = False

            def source(self):
                return self._source

            def mimeData(self):
                return self._mime_data

            def position(self):
                return self._position

            def setDropAction(self, action) -> None:
                self.action = action

            def accept(self) -> None:
                self.accepted = True

            def ignore(self) -> None:
                self.accepted = False

        class FakeDrag:
            def __init__(self, source) -> None:
                self.source = source
                self.mime_data = None

            def setMimeData(self, mime_data) -> None:
                self.mime_data = mime_data

            def setPixmap(self, _pixmap) -> None:
                pass

            def exec(self, *_args):
                target_rect = self.source.visualItemRect(self.source.item(self.source.count() - 1))
                event = FakeDropEvent(self.source, self.mime_data, target_rect.center())
                self.source.dropEvent(event)
                counts_during_drop.append(self.source.count())
                self.assert_drop_accepted = event.accepted
                return Qt.DropAction.MoveAction

        with patch("pyside6_frame_viewer.QDrag", FakeDrag), patch.object(QListWidget, "startDrag") as default_drag:
            timeline.startDrag(Qt.DropAction.MoveAction)
            default_drag.assert_not_called()

        self.assertEqual(counts_during_drop, [len(original_ids)])
        self.assertEqual(timeline.count(), len(original_ids))
        self.assertEqual(set(frame.frame_id for frame in self.window.frames), set(original_ids))
        self.assertEqual(self.window.frames[-1].frame_id, source_id)
        self.assertEqual(self.window.frames[self.window.current_index].frame_id, source_id)
        for frame in self.window.frames:
            self.assertIs(frame, original_records[frame.frame_id])
            item = self.window._timeline_item_for_frame_id(frame.frame_id)
            self.assertEqual(item.icon().cacheKey(), original_icon_keys[frame.frame_id])

    def test_rapid_timeline_selection_keeps_only_last_pending_frame(self) -> None:
        self.window._frame_switch_timer.stop()
        self.window._pending_timeline_frame_id = None
        self.window._timeline_row_changed(1)
        self.window._timeline_row_changed(2)
        self.window._timeline_row_changed(3)
        self.window._apply_pending_timeline_frame()

        self.assertEqual(self.window.current_index, 3)
        self.window._preload_adjacent_frames()
        self.assertIn(2, self.window._frame_pixmap_cache)
        self.assertIn(4, self.window._frame_pixmap_cache)

    def test_thirty_rapid_timeline_requests_apply_only_the_last_frame(self) -> None:
        self.window.frame_paths = [Path(f"frame_{index}.png") for index in range(30)]
        self.window._load_timeline()
        requested: list[int] = []
        self.window.set_current_frame = lambda index, **_kwargs: requested.append(index) or True  # type: ignore[method-assign]
        self.window._frame_switch_timer.stop()
        for index in range(30):
            self.window._timeline_row_changed(index)
        self.window._frame_switch_timer.stop()
        self.window._apply_pending_timeline_frame()
        self.assertEqual(requested, [29])

    def test_timeline_drop_insertion_uses_left_right_halves_and_end_gap(self) -> None:
        self._prepare_structural_frames()
        self.window.show()
        self.app.processEvents()
        timeline = self.window.timeline
        target = timeline.item(1)
        target_id = str(target.data(0x0100))
        rect = timeline.visualItemRect(target)
        self.assertEqual(timeline._anchor_for_position(QPoint(rect.left() + 1, rect.center().y())), (target_id, False))
        self.assertEqual(timeline._anchor_for_position(QPoint(rect.right() - 1, rect.center().y())), (target_id, True))
        last_rect = timeline.visualItemRect(timeline.item(timeline.count() - 1))
        self.assertEqual(timeline._anchor_for_position(QPoint(last_rect.right() + 20, last_rect.center().y())), ("", True))

    def test_failed_thumbnail_keeps_fixed_placeholder_item(self) -> None:
        frame = self.window.frames[0]
        frame.path = Path("missing-thumbnail-source.png")
        frame.reference_image = None
        frame.thumbnail_reference = None
        frame.thumbnail_cache.clear()
        frame.thumbnail_dirty = True
        self.window._load_timeline()
        self.window._update_timeline_item_thumbnail(frame.frame_id, self.window._thumbnail_generation)
        item = self.window._timeline_item_for_frame_id(frame.frame_id)
        self.assertIsNotNone(item)
        self.assertEqual(item.sizeHint(), self.window.timeline.gridSize())
        self.assertFalse(item.icon().isNull())
        self.assertIn(self.window.thumbnail_display_mode, frame.thumbnail_cache)

    def test_thumbnail_mode_and_view_mode_keep_only_last_request(self) -> None:
        self.window.thumbnail_display_combo.setCurrentIndex(0)
        self.window.thumbnail_display_combo.setCurrentIndex(1)
        self.window.thumbnail_display_combo.setCurrentIndex(2)
        self.window._apply_pending_thumbnail_display_mode()
        self.assertEqual(self.window.thumbnail_display_mode, "composite")

        requested: list[int] = []
        self.window.set_view_mode = lambda index: requested.append(index)  # type: ignore[method-assign]
        self.window.request_view_mode(0)
        self.window.request_view_mode(1)
        self.window.request_view_mode(2)
        self.window._view_switch_timer.stop()
        self.window._apply_pending_view_mode()
        self.assertEqual(requested, [2])

    def test_project_round_trip_preserves_frame_order_exposure_and_layers(self) -> None:
        self._prepare_structural_frames()
        self.window._reorder_frames([2, 0, 1])
        self.window.onion_loop_checkbox.setChecked(True)
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "timeline.giftrace"
            self.assertTrue(self.window._write_project(project_path))
            with zipfile.ZipFile(project_path) as archive:
                manifest = __import__("json").loads(archive.read("manifest.json"))
            self.assertEqual(manifest["frame_exposures"], [3, 1, 2])
            self.assertEqual(manifest["version"], 4)
            self.assertEqual(manifest["frame_ids"], [frame.frame_id for frame in self.window.frames])
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(project_path), "")):
                self.window.open_project()
        self.assertEqual(self.window.frame_exposures, [3, 1, 2])
        self.assertTrue(self.window.onion_loop_checkbox.isChecked())
        self.assertEqual(self.window._project_reference_images[0].pixelColor(0, 0), QColor("#2563eb"))
        self.assertTrue(self._contains_color(self.window.drawing_layers[0], QColor("#2563eb")))

    def test_onion_loop_neighbors_and_single_frame(self) -> None:
        self._prepare_structural_frames()
        self.window.onion_loop_checkbox.setChecked(False)
        self.assertIsNone(self.window._onion_neighbor_index(0, -1))
        self.assertIsNone(self.window._onion_neighbor_index(2, 1))
        self.window.onion_loop_checkbox.setChecked(True)
        self.assertEqual(self.window._onion_neighbor_index(0, -1), 2)
        self.assertEqual(self.window._onion_neighbor_index(2, 1), 0)
        self.window.frame_paths = self.window.frame_paths[:1]
        self.assertIsNone(self.window._onion_neighbor_index(0, -1))
        self.assertIsNone(self.window._onion_neighbor_index(0, 1))

    def test_layer_groups_keep_independent_frame_drawings_and_visibility(self) -> None:
        frame_id = self.window.frames[0].frame_id
        red = self._fill_rect(self._blank_image(), QRect(2, 2, 8, 8), QColor("#ef4444"))
        self.window.drawing_layers[0] = red
        original_group_id = self.window.active_layer_group_id

        self.window.add_layer_group()
        new_group_id = self.window.layer_groups[0].group_id
        self.window._activate_layer_group(new_group_id)
        blue = self._fill_rect(self._blank_image(), QRect(2, 2, 8, 8), QColor("#2563eb"))
        self.window.drawing_layers[0] = blue
        self.window._store_active_layer_group()

        self.window._activate_layer_group(original_group_id)
        self.assertTrue(self._contains_color(self.window.drawing_layers[0], QColor("#ef4444")))
        composite = self.window._composited_visible_drawing(frame_id, QSize(64, 64))
        self.assertEqual(composite.pixelColor(4, 4), QColor("#2563eb"))

        self.window.layer_groups[0].visible = False
        composite = self.window._composited_visible_drawing(frame_id, QSize(64, 64))
        self.assertEqual(composite.pixelColor(4, 4), QColor("#ef4444"))

    def test_delayed_stroke_from_previous_group_cannot_write_into_active_group(self) -> None:
        canvas = self.window.trace_only_canvas
        before = self._blank_image()
        canvas.set_drawing_image(before)
        canvas.stroke_started.emit(before)

        self.window.add_layer_group()
        active_group_id = self.window.active_layer_group_id
        rect = QRect(4, 4, 6, 6)
        after_patch = QImage(rect.size(), QImage.Format.Format_ARGB32_Premultiplied)
        after_patch.fill(QColor("#111111"))
        before_patch = QImage(rect.size(), QImage.Format.Format_ARGB32_Premultiplied)
        before_patch.fill(Qt.GlobalColor.transparent)
        canvas.drawing_changed.emit("brush", rect, before_patch, after_patch)

        self.assertEqual(self.window.active_layer_group_id, active_group_id)
        active = self.window._drawing_image_for_current_frame()
        self.assertFalse(self._contains_color(active, QColor("#111111")))

    def test_hiding_active_group_clears_preview_and_keeps_drawing_item_hidden(self) -> None:
        canvas = self.window.trace_only_canvas
        canvas.set_drawing_image(self._fill_rect(self._blank_image(), QRect(2, 2, 8, 8), QColor("#ef4444")))
        canvas._create_stroke_preview_layer()
        canvas._stroke_preview_image.fill(QColor("#2563eb"))
        canvas._refresh_stroke_preview_item()
        self.assertTrue(canvas._stroke_preview_item.isVisible())

        item = self.window.layer_group_list.currentItem()
        item.setCheckState(Qt.CheckState.Unchecked)
        self.assertFalse(canvas._drawing_item.isVisible())
        self.assertFalse(canvas._stroke_preview_item.isVisible())
        canvas._clear_stroke_preview_layer()
        self.assertFalse(canvas._drawing_item.isVisible())

    def test_layer_group_list_supports_rename_and_drag_order(self) -> None:
        self.window.add_layer_group()
        item = self.window.layer_group_list.item(0)
        item.setText("前景动画")
        self.assertEqual(self.window.layer_groups[0].name, "前景动画")
        self.assertEqual(
            self.window.layer_group_list.dragDropMode(),
            QAbstractItemView.DragDropMode.InternalMove,
        )
        self.assertTrue(bool(item.flags() & Qt.ItemFlag.ItemIsEditable))
        self.assertTrue(bool(item.flags() & Qt.ItemFlag.ItemIsUserCheckable))

    def test_timeline_displays_one_synchronized_row_per_layer_group(self) -> None:
        original_group_id = self.window.active_layer_group_id
        self.window.add_layer_group()
        self.assertEqual(len(self.window.group_timeline_widgets), 2)
        for timeline in self.window.group_timeline_widgets.values():
            self.assertEqual(timeline.count(), len(self.window.frames))

        original_timeline = self.window.group_timeline_widgets[original_group_id]
        original_timeline.setCurrentRow(1)
        self.window._frame_switch_timer.stop()
        self.window._apply_pending_timeline_frame()
        self.assertEqual(self.window.active_layer_group_id, original_group_id)
        self.assertEqual(self.window.current_index, 1)

        source_id = self.window.frames[0].frame_id
        last_id = self.window.frames[-1].frame_id
        self.assertTrue(self.window._move_frame_by_id(source_id, last_id, True))
        expected_ids = [frame.frame_id for frame in self.window.frames]
        for timeline in self.window.group_timeline_widgets.values():
            actual_ids = [
                str(timeline.item(index).data(Qt.ItemDataRole.UserRole))
                for index in range(timeline.count())
            ]
            self.assertEqual(actual_ids, expected_ids)

    def test_timeline_group_highlight_zoom_and_layer_selection_focus_stay_in_sync(self) -> None:
        original_group_id = self.window.active_layer_group_id
        self.window.add_layer_group()
        self.window.current_index = 3
        original_item = next(
            self.window.layer_group_list.item(index)
            for index in range(self.window.layer_group_list.count())
            if str(self.window.layer_group_list.item(index).data(Qt.ItemDataRole.UserRole)) == original_group_id
        )
        self.window.layer_group_list.setCurrentItem(original_item)

        self.assertEqual(self.window.active_layer_group_id, original_group_id)
        active_timeline = self.window.group_timeline_widgets[original_group_id]
        self.assertEqual(
            str(active_timeline.currentItem().data(Qt.ItemDataRole.UserRole)),
            self.window.frames[3].frame_id,
        )
        self.assertIn("#3b82f6", self.window.group_timeline_row_hosts[original_group_id].styleSheet())

        self.window.timeline_zoom_slider.setValue(150)
        self.assertEqual(self.window.timeline_zoom_value_label.text(), "150%")
        self.assertEqual(self.window.timeline.iconSize(), QSize(96, 54))
        self.assertEqual(self.window.timeline.gridSize(), QSize(162, 82))
        for timeline in self.window.group_timeline_widgets.values():
            self.assertEqual(timeline.iconSize(), QSize(96, 54))
            self.assertEqual(timeline.item(0).sizeHint(), QSize(162, 82))
        self.assertEqual(self.window.timeline.item(0).text(), "1")
        self.assertEqual(self.window.timeline.item(1).text(), "2")
        self.window.timeline_zoom_slider.setValue(50)
        self.assertEqual(self.window.timeline.iconSize(), QSize(96, 54))
        self.assertEqual(self.window.timeline.gridSize(), QSize(54, 82))
        self.assertLess(self.window.timeline.gridSize().width(), self.window.timeline.iconSize().width())
        self.window.timeline_zoom_slider.setValue(100)

    def test_onion_skin_uses_only_active_group_and_top_number_switches_frame(self) -> None:
        self.window.current_drawing_size = QSize(64, 64)
        original_group_id = self.window.active_layer_group_id
        red = self._fill_rect(self._blank_image(), QRect(2, 2, 8, 8), QColor("#ef4444"))
        self.window.drawing_layers[0] = red
        self.window.add_layer_group()
        new_group_id = self.window.layer_groups[0].group_id
        self.window._activate_layer_group(new_group_id)
        blue = self._fill_rect(self._blank_image(), QRect(20, 20, 8, 8), QColor("#2563eb"))
        self.window.drawing_layers[0] = blue
        self.window._store_active_layer_group()

        self.window._activate_layer_group(original_group_id)
        onion = self.window._drawing_onion_image_for_frame(0)
        self.assertTrue(self._contains_color(onion, QColor("#ef4444")))
        self.assertFalse(self._contains_color(onion, QColor("#2563eb")))

        self.window.show()
        self.app.processEvents()
        base_x, span = self.window.timeline_ruler._sequence_geometry()
        QTest.mouseClick(
            self.window.timeline_ruler,
            Qt.MouseButton.LeftButton,
            pos=QPoint(round(base_x + 2 * span), self.window.timeline_ruler.height() // 2),
        )
        self.window._frame_switch_timer.stop()
        self.window._apply_pending_timeline_frame()
        self.assertEqual(self.window.current_index, 2)
        expected_frame_id = self.window.frames[2].frame_id
        for timeline in self.window.group_timeline_widgets.values():
            self.assertEqual(str(timeline.currentItem().data(Qt.ItemDataRole.UserRole)), expected_frame_id)

    def test_timeline_has_infinite_floating_ruler_and_resizable_height(self) -> None:
        self.window.show()
        self.app.processEvents()
        self.assertIs(self.window.timeline_ruler._timeline, self.window.timeline)
        base_x, span = self.window.timeline_ruler._sequence_geometry()
        self.assertGreater(span, 0)
        self.assertGreater(self.window.timeline_ruler.width(), 0)

        before = self.window.canvas_timeline_splitter.sizes()
        self.window.canvas_timeline_splitter.moveSplitter(max(180, before[0] - 80), 1)
        self.app.processEvents()
        after = self.window.canvas_timeline_splitter.sizes()
        self.assertNotEqual(before, after)
        self.assertGreater(after[1], 0)
        self.assertLessEqual(self.window.timeline.height(), 132)

        ruler_image = self.window.timeline_ruler.grab().toImage()
        dark_pixels = sum(
            max(
                ruler_image.pixelColor(x, y).red(),
                ruler_image.pixelColor(x, y).green(),
                ruler_image.pixelColor(x, y).blue(),
            ) < 160
            for y in range(ruler_image.height())
            for x in range(ruler_image.width())
        )
        self.assertGreater(dark_pixels, ruler_image.width() * ruler_image.height() // 2)

    def test_playback_range_handles_are_draggable_and_limit_preview(self) -> None:
        self.window.show()
        self.app.processEvents()
        ruler = self.window.timeline_ruler
        base_x, span = ruler._sequence_geometry()

        QTest.mousePress(ruler, Qt.MouseButton.LeftButton, pos=QPoint(round(base_x - 0.5 * span), ruler.height() // 2))
        QTest.mouseMove(ruler, QPoint(round(base_x + 1.5 * span), ruler.height() // 2))
        QTest.mouseRelease(ruler, Qt.MouseButton.LeftButton, pos=QPoint(round(base_x + 1.5 * span), ruler.height() // 2))
        self.assertEqual(self.window.playback_range_start, 2)

        end_x = base_x + (self.window.playback_range_end + 0.5) * span
        QTest.mousePress(ruler, Qt.MouseButton.LeftButton, pos=QPoint(round(end_x), ruler.height() // 2))
        QTest.mouseMove(ruler, QPoint(round(base_x + 4.5 * span), ruler.height() // 2))
        QTest.mouseRelease(ruler, Qt.MouseButton.LeftButton, pos=QPoint(round(base_x + 4.5 * span), ruler.height() // 2))
        self.assertEqual(self.window.playback_range_end, 4)

        self.window.current_index = 0
        self.window.toggle_playback(True)
        self.window.play_timer.stop()
        self.assertEqual(self.window.current_index, 2)
        self.window.advance_playback()
        self.window.play_timer.stop()
        self.assertEqual(self.window.current_index, 3)
        self.window.advance_playback()
        self.window.play_timer.stop()
        self.assertEqual(self.window.current_index, 4)
        self.window.advance_playback()
        self.window.play_timer.stop()
        self.assertEqual(self.window.current_index, 2)
        self.window.toggle_playback(False)

    def test_project_round_trip_preserves_multiple_layer_groups(self) -> None:
        self.window.current_drawing_size = QSize(64, 64)
        first_id = self.window.active_layer_group_id
        self.window.drawing_layers[0] = self._fill_rect(self._blank_image(), QRect(1, 1, 4, 4), QColor("#ef4444"))
        self.window.add_layer_group()
        second_id = self.window.layer_groups[0].group_id
        self.window._activate_layer_group(second_id)
        self.window.drawing_layers[0] = self._fill_rect(self._blank_image(), QRect(8, 8, 4, 4), QColor("#2563eb"))
        self.window.layer_groups[1].visible = False
        self.window.layer_groups[0].name = "高光"
        self.window.set_playback_range(1, 4)

        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "layers.giftrace"
            self.assertTrue(self.window._write_project(project_path))
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(project_path), "")):
                self.window.open_project()

        self.assertEqual([group.name for group in self.window.layer_groups], ["高光", "图层组 1"])
        self.assertEqual([group.visible for group in self.window.layer_groups], [True, False])
        self.assertEqual(self.window.active_layer_group_id, second_id)
        self.assertEqual((self.window.playback_range_start, self.window.playback_range_end), (1, 4))
        self.assertTrue(self._contains_color(self.window.drawing_layers[0], QColor("#2563eb")))
        restored_first = next(group for group in self.window.layer_groups if group.group_id == first_id)
        self.assertTrue(self._contains_color(restored_first.drawings[self.window.frames[0].frame_id], QColor("#ef4444")))


if __name__ == "__main__":
    unittest.main()
