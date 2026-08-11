from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def build_icon(source: Path, destination: Path) -> None:
    with Image.open(source) as image:
        rgba = image.convert("RGBA")
        if rgba.width != rgba.height:
            raise ValueError("launcher icon source must be square")
        alpha = rgba.getchannel("A")
        minimum, maximum = alpha.getextrema()
        if minimum != 0 or maximum != 255:
            raise ValueError("launcher icon source must contain real transparency")
        destination.parent.mkdir(parents=True, exist_ok=True)
        rgba.save(destination, format="ICO", sizes=[(size, size) for size in ICON_SIZES])


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Workbench multi-size Windows icon.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_icon(args.source.resolve(), args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
