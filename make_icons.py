"""Generate the icons of Corridoor (standard library only).

Each icon is a white door on a colored disc with a symbol cut out of it:
``run.ico`` a "play" triangle on green, ``hop.ico`` a monitor on orange,
``files.ico`` two opposite arrows on red, ``config.ico`` a gear on blue.
Each file holds several sizes as embedded PNGs. The same pictures are also written
as ``docs/images/icons/*.png`` (the .ico files are not tracked by git, the PNGs are,
so that the README can show them).
"""

import math
import struct
import zlib
from pathlib import Path
from typing import Callable

SIZES: tuple[int, ...] = (16, 32, 48, 256)
SAMPLES: int = 4  # sub-pixels per axis for smooth edges

Color = tuple[int, int, int]
Shape = Callable[[float, float], bool]  # (x, y) in -1..1 -> inside the symbol?


def play(x: float, y: float) -> bool:
    """Triangle pointing to the right."""
    return -0.35 <= x <= 0.55 and abs(y) <= (0.55 - x) * 0.6


def gear(x: float, y: float) -> bool:
    """Ring with eight teeth."""
    radius = math.hypot(x, y)
    outer = 0.66 if math.cos(8 * math.atan2(y, x)) > 0 else 0.50
    return 0.22 <= radius <= outer


def monitor(x: float, y: float) -> bool:
    """Screen frame with a stand."""
    frame = abs(x) <= 0.62 and -0.48 <= y <= 0.30
    screen = abs(x) <= 0.48 and -0.34 <= y <= 0.16
    stand = abs(x) <= 0.10 and 0.30 <= y <= 0.50
    base = abs(x) <= 0.34 and 0.46 <= y <= 0.58
    return (frame and not screen) or stand or base


def transfer(x: float, y: float) -> bool:
    """Two opposite arrows: one up on the left, one down on the right."""
    up_head = -0.62 <= y <= -0.12 and abs(x + 0.3) <= (y + 0.62) * 0.7
    up_stem = abs(x + 0.3) <= 0.10 and -0.12 <= y <= 0.62
    down_head = 0.12 <= y <= 0.62 and abs(x - 0.3) <= (0.62 - y) * 0.7
    down_stem = abs(x - 0.3) <= 0.10 and -0.62 <= y <= 0.12
    return up_head or up_stem or down_head or down_stem


def door(symbol: Shape) -> Shape:
    """An arched door with a smaller symbol cut out of it."""
    def shape(x: float, y: float) -> bool:
        arch = math.hypot(x, y + 0.20) <= 0.52 if y < -0.20 else abs(x) <= 0.52
        return arch and y <= 0.70 and not symbol(x / 0.65, (y - 0.08) / 0.65)

    return shape


def png(size: int, background: Color, shape: Shape) -> bytes:
    """Render a white shape on a colored disc as an RGBA PNG."""
    rows = bytearray()
    for py in range(size):
        rows.append(0)  # PNG filter type "none" at the start of every row
        for px in range(size):
            inside = symbol = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    x = (px + (sx + 0.5) / SAMPLES) / size * 2 - 1
                    y = (py + (sy + 0.5) / SAMPLES) / size * 2 - 1
                    if math.hypot(x, y) <= 0.97:
                        inside += 1
                        symbol += shape(x, y)
            white = symbol / inside if inside else 0.0
            rows += bytes(round(c + (255 - c) * white) for c in background)
            rows.append(round(255 * inside / SAMPLES ** 2))  # alpha

    def chunk(kind: bytes, data: bytes) -> bytes:
        """Build one PNG chunk with its checksum."""
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8 bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))


def write_ico(path: Path, background: Color, shape: Shape) -> None:
    """Pack the PNG images of all sizes into one .ico file."""
    images = [png(size, background, shape) for size in SIZES]
    offset = 6 + 16 * len(images)  # file header plus the directory entries
    directory = struct.pack("<HHH", 0, 1, len(images))
    for size, image in zip(SIZES, images):
        # A width/height byte of 0 means 256 pixels.
        directory += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                                 len(image), offset)
        offset += len(image)
    path.write_bytes(directory + b"".join(images))


ICONS: tuple[tuple[str, Color, Shape], ...] = (
    ("run", (34, 150, 80), door(play)),
    ("hop", (220, 120, 30), door(monitor)),
    ("files", (190, 40, 40), door(transfer)),
    ("config", (40, 105, 190), door(gear)),
)
PICTURE_SIZE: int = 128  # pixels of the PNGs for the documentation

if __name__ == "__main__":
    folder = Path(__file__).parent
    pictures = folder / "docs" / "images" / "icons"
    pictures.mkdir(parents=True, exist_ok=True)
    for name, color, shape in ICONS:
        write_ico(folder / f"{name}.ico", color, shape)
        (pictures / f"{name}.png").write_bytes(png(PICTURE_SIZE, color, shape))
