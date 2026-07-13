# Timeline drag-and-drop root cause

The frame loss was not a thumbnail rendering problem.  The timeline had two
owners for one drag operation:

1. `QListWidget` was configured with `InternalMove`, which gives Qt's item
   model permission to move items during an internal drag.
2. `TimelineListWidget.dropEvent()` also derived a new order and asked the
   window to reorder `FrameData`.

The first attempted fix changed the view from `InternalMove` to `DragDrop` but
still called `QListWidget.startDrag()`.  That default implementation still owns
the model drag lifecycle and can remove the source item after an accepted
`MoveAction`.  It therefore did not actually remove Qt's move path.

Those mechanisms can observe and mutate the item order at different points in
the drag lifecycle.  Thumbnail refresh then used `timeline.item(index)` and
`frames[index]` as if the row was a stable identity.  Once the widget order and
the `FrameData` order differed, a delayed refresh could generate the thumbnail
for one frame and write it to another item.  Rebuilding after that mismatch
made the apparent source frame disappear or made an item fall back to text-only
content.  In addition, every successful reorder called `timeline.clear()` and
rebuilt all items with placeholder icons.  Even when the data order was
correct, this guaranteed a visible disappear/reappear flash until thumbnail
refresh restored the icons.

The fix keeps `FrameData.frame_id` as the only persistent identity.  The
timeline is a projection of the `FrameData` list.  A standalone `QDrag` carries
only the source frame id; no `QListWidget.startDrag()` or model move runs.  The
drop computes an anchor frame id and before/after placement, and the move is
submitted only after the drag operation has ended.  After validating and
atomically reordering the data list, existing timeline items are synchronized
in place with updates disabled, retaining their icons throughout.  A full
clear/rebuild is reserved for structural changes such as insert and delete.
Thumbnail cache lookup and every pending refresh use frame ids and a generation
guard, never a row as a lasting reference.
