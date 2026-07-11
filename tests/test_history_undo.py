from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, QSize
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

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

    def _commit_image_change(self, kind: str, before: QImage, after: QImage) -> None:
        self.window._pending_stroke_before[0] = (kind, before)
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

    def test_global_memory_limit_evicts_oldest_operation_across_frames(self) -> None:
        self.window._history_memory = HistoryMemoryManager(limit_bytes=8)
        first_before = self._blank_image()
        first_after = self._fill_rect(first_before, QRect(1, 1, 1, 1), QColor("black"))
        self._commit_image_change("brush", first_before, first_after)

        self.window.current_index = 1
        self.window.drawing_layers[1] = self._blank_image()
        second_before = self._blank_image()
        second_after = self._fill_rect(second_before, QRect(2, 2, 1, 1), QColor("black"))
        self.window._pending_stroke_before[1] = ("brush", second_before)
        self.window.update_current_drawing_layer(second_after)

        self.assertFalse(self.window.frame_histories[0].undo)
        self.assertEqual(len(self.window.frame_histories[1].undo), 1)
        self.assertLessEqual(self.window._history_memory.total_bytes, 8)


if __name__ == "__main__":
    unittest.main()
