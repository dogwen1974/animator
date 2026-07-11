from __future__ import annotations

import argparse
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageSequence


@dataclass(frozen=True)
class GifFrame:
    """One extracted GIF frame."""

    image: Image.Image
    duration: int
    index: int


def load_gif_frames(gif_path: str | Path, step: int = 1) -> list[GifFrame]:
    """Load selected frames from a GIF as RGBA PIL Images.

    Args:
        gif_path: Path to the input GIF file.
        step: Keep every Nth frame. For example, step=2 keeps frames
            0, 2, 4, ... .

    Returns:
        A list of GifFrame objects. Each image is a copied RGBA PIL Image,
        so it remains usable after the GIF file is closed.
    """

    if step < 1:
        raise ValueError("step must be a positive integer")

    gif_path = Path(gif_path)
    frames: list[GifFrame] = []

    with Image.open(gif_path) as gif:
        for index, frame in enumerate(ImageSequence.Iterator(gif)):
            if index % step != 0:
                continue

            duration = int(frame.info.get("duration", gif.info.get("duration", 0)) or 0)
            rgba_image = frame.convert("RGBA").copy()
            frames.append(GifFrame(image=rgba_image, duration=duration, index=index))

    return frames


def export_png_sequence(
    frames: Iterable[GifFrame],
    output_dir: str | Path,
    prefix: str = "frame",
    digits: int = 4,
) -> list[Path]:
    """Export GIF frames as a numbered PNG sequence.

    Args:
        frames: GifFrame values returned by load_gif_frames.
        output_dir: Directory where PNG files will be written.
        prefix: Filename prefix.
        digits: Zero-padding width for frame numbers.

    Returns:
        Paths to the PNG files written.
    """

    if digits < 1:
        raise ValueError("digits must be a positive integer")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    written_paths: list[Path] = []
    for output_index, gif_frame in enumerate(frames):
        output_path = output_dir / f"{prefix}_{output_index:0{digits}d}.png"
        gif_frame.image.save(output_path, format="PNG")
        written_paths.append(output_path)

    return written_paths


def _create_test_gif(gif_path: Path) -> list[int]:
    durations = [40, 80, 120, 160]
    frames = [
        Image.new("RGBA", (16, 16), (255, 0, 0, 255)),
        Image.new("RGBA", (16, 16), (0, 255, 0, 180)),
        Image.new("RGBA", (16, 16), (0, 0, 255, 128)),
        Image.new("RGBA", (16, 16), (255, 255, 0, 255)),
    ]

    frames[0].save(
        gif_path,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
    )
    return durations


def run_tests() -> None:
    """Run a small self-test using a generated GIF."""

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        gif_path = tmp_path / "sample.gif"
        expected_durations = _create_test_gif(gif_path)

        frames = load_gif_frames(gif_path)
        assert len(frames) == 4
        assert [frame.duration for frame in frames] == expected_durations
        assert [frame.index for frame in frames] == [0, 1, 2, 3]
        assert all(frame.image.mode == "RGBA" for frame in frames)

        stepped_frames = load_gif_frames(gif_path, step=2)
        assert len(stepped_frames) == 2
        assert [frame.duration for frame in stepped_frames] == [40, 120]
        assert [frame.index for frame in stepped_frames] == [0, 2]

        output_dir = tmp_path / "png_frames"
        png_paths = export_png_sequence(stepped_frames, output_dir, prefix="test")
        assert len(png_paths) == 2
        assert all(path.exists() for path in png_paths)
        assert [path.name for path in png_paths] == ["test_0000.png", "test_0001.png"]

        try:
            load_gif_frames(gif_path, step=0)
        except ValueError:
            pass
        else:
            raise AssertionError("step=0 should raise ValueError")

    print("All tests passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Load GIF frames as RGBA images.")
    parser.add_argument("gif", nargs="?", help="Path to a GIF file.")
    parser.add_argument("--step", type=int, default=1, help="Keep every Nth frame.")
    parser.add_argument("--export-dir", help="Optional directory for exported PNGs.")
    parser.add_argument("--prefix", default="frame", help="PNG filename prefix.")
    parser.add_argument("--test", action="store_true", help="Run the built-in tests.")
    args = parser.parse_args()

    if args.test:
        run_tests()
        return

    if not args.gif:
        parser.error("gif is required unless --test is used")

    frames = load_gif_frames(args.gif, step=args.step)
    print(f"Loaded {len(frames)} frame(s).")

    for frame in frames:
        print(f"frame={frame.index} duration={frame.duration}ms size={frame.image.size}")

    if args.export_dir:
        paths = export_png_sequence(frames, args.export_dir, prefix=args.prefix)
        print(f"Exported {len(paths)} PNG file(s) to {Path(args.export_dir)}")


if __name__ == "__main__":
    main()
