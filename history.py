from __future__ import annotations

from collections import deque
from collections.abc import Hashable, Mapping
from dataclasses import dataclass, field

from PySide6.QtCore import QRect
from PySide6.QtGui import QImage


MAX_HISTORY_OPERATIONS = 100
DEFAULT_HISTORY_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024


@dataclass
class DrawingOperation:
    """A reversible change stored as before/after image patches for one QRect."""

    kind: str
    rect: QRect
    before_patch: QImage
    after_patch: QImage
    sequence: int = 0
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def memory_bytes(self) -> int:
        return self.before_patch.sizeInBytes() + self.after_patch.sizeInBytes()


@dataclass
class FrameDrawingHistory:
    """Undo and redo stacks for a single animation frame."""

    undo: list[DrawingOperation] = field(default_factory=list)
    redo: list[DrawingOperation] = field(default_factory=list)

    def commit(self, operation: DrawingOperation) -> list[DrawingOperation]:
        discarded = [*self.redo]
        self.redo.clear()
        self.undo.append(operation)
        while len(self.undo) > MAX_HISTORY_OPERATIONS:
            discarded.append(self.undo.pop(0))
        return discarded

    def remove(self, sequence: int) -> DrawingOperation | None:
        for stack in (self.undo, self.redo):
            for index, operation in enumerate(stack):
                if operation.sequence == sequence:
                    return stack.pop(index)
        return None


class HistoryMemoryManager:
    """Tracks all frame histories and evicts the oldest operations by memory use."""

    def __init__(self, limit_bytes: int = DEFAULT_HISTORY_MEMORY_LIMIT_BYTES) -> None:
        self.limit_bytes = max(0, int(limit_bytes))
        self.total_bytes = 0
        self._next_sequence = 1
        self._order: deque[tuple[Hashable, int]] = deque()

    def commit(
        self,
        frame_index: Hashable,
        history: FrameDrawingHistory,
        operation: DrawingOperation,
        histories: Mapping[Hashable, FrameDrawingHistory],
    ) -> None:
        operation.sequence = self._next_sequence
        self._next_sequence += 1
        self.total_bytes += operation.memory_bytes
        self._order.append((frame_index, operation.sequence))
        self._discard(history.commit(operation))
        self.enforce_limit(histories)

    def reset(self) -> None:
        self.total_bytes = 0
        self._next_sequence = 1
        self._order.clear()

    def recalculate(self, histories: Mapping[Hashable, FrameDrawingHistory]) -> None:
        self.total_bytes = sum(
            operation.memory_bytes
            for history in histories.values()
            for operation in (*history.undo, *history.redo)
        )
        self.enforce_limit(histories)

    def reindex(self, histories: Mapping[Hashable, FrameDrawingHistory]) -> None:
        """Rebuild frame-index references after timeline insertions or moves."""

        operations = [
            (operation.sequence, frame_index)
            for frame_index, history in histories.items()
            for operation in (*history.undo, *history.redo)
            if operation.sequence > 0
        ]
        operations.sort()
        self._order = deque((frame_index, sequence) for sequence, frame_index in operations)
        self.total_bytes = sum(
            operation.memory_bytes
            for history in histories.values()
            for operation in (*history.undo, *history.redo)
        )
        self.enforce_limit(histories)

    def enforce_limit(self, histories: Mapping[Hashable, FrameDrawingHistory]) -> None:
        while self.total_bytes > self.limit_bytes and self._order:
            frame_index, sequence = self._order.popleft()
            history = histories.get(frame_index)
            if history is None:
                continue
            operation = history.remove(sequence)
            if operation is not None:
                self.total_bytes = max(0, self.total_bytes - operation.memory_bytes)

    def _discard(self, operations: list[DrawingOperation]) -> None:
        for operation in operations:
            self.total_bytes = max(0, self.total_bytes - operation.memory_bytes)
