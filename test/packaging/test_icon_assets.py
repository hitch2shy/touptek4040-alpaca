from __future__ import annotations

import struct
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PNG_PATH = ROOT / "assets" / "touptek4040-icon.png"
ICO_PATH = ROOT / "assets" / "touptek4040.ico"
BUILD_SCRIPT = ROOT / "test" / "scripts" / "build_icon.ps1"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
EXPECTED_ICON_SIZES = {16, 24, 32, 48, 64, 128, 256}


@dataclass(frozen=True)
class IcoEntry:
    width: int
    height: int
    payload: bytes


def _png_chunks(payload: bytes) -> list[tuple[bytes, bytes]]:
    assert payload.startswith(PNG_SIGNATURE)
    chunks: list[tuple[bytes, bytes]] = []
    offset = len(PNG_SIGNATURE)
    while offset < len(payload):
        length = struct.unpack_from(">I", payload, offset)[0]
        kind = payload[offset + 4 : offset + 8]
        data = payload[offset + 8 : offset + 8 + length]
        chunks.append((kind, data))
        offset += 12 + length
        if kind == b"IEND":
            break
    return chunks


def _rgba_rows(payload: bytes) -> tuple[int, int, list[bytearray]]:
    chunks = _png_chunks(payload)
    ihdr = next(data for kind, data in chunks if kind == b"IHDR")
    width, height, bit_depth, color_type, compression, filtering, interlace = (
        struct.unpack(">IIBBBBB", ihdr)
    )
    assert (bit_depth, color_type, compression, filtering, interlace) == (
        8,
        6,
        0,
        0,
        0,
    )
    packed = zlib.decompress(b"".join(data for kind, data in chunks if kind == b"IDAT"))
    stride = width * 4
    rows: list[bytearray] = []
    previous = bytearray(stride)
    offset = 0
    for _ in range(height):
        filter_type = packed[offset]
        raw = packed[offset + 1 : offset + 1 + stride]
        row = bytearray(stride)
        for index, value in enumerate(raw):
            left = row[index - 4] if index >= 4 else 0
            up = previous[index]
            upper_left = previous[index - 4] if index >= 4 else 0
            if filter_type == 0:
                decoded = value
            elif filter_type == 1:
                decoded = value + left
            elif filter_type == 2:
                decoded = value + up
            elif filter_type == 3:
                decoded = value + ((left + up) // 2)
            elif filter_type == 4:
                estimate = left + up - upper_left
                distances = (
                    abs(estimate - left),
                    abs(estimate - up),
                    abs(estimate - upper_left),
                )
                decoded = value + (left, up, upper_left)[distances.index(min(distances))]
            else:
                raise AssertionError(f"unsupported PNG filter {filter_type}")
            row[index] = decoded & 0xFF
        rows.append(row)
        previous = row
        offset += stride + 1
    return width, height, rows


def _ico_entries(payload: bytes) -> list[IcoEntry]:
    reserved, image_type, count = struct.unpack_from("<HHH", payload)
    assert (reserved, image_type) == (0, 1)
    entries: list[IcoEntry] = []
    for index in range(count):
        offset = 6 + index * 16
        width_byte, height_byte, _colors, _reserved, planes, bits, size, start = (
            struct.unpack_from("<BBBBHHII", payload, offset)
        )
        assert (planes, bits) == (1, 32)
        entries.append(
            IcoEntry(
                width=width_byte or 256,
                height=height_byte or 256,
                payload=payload[start : start + size],
            )
        )
    return entries


def test_icon_source_is_square_rgba_png_with_transparent_outer_corners() -> None:
    payload = PNG_PATH.read_bytes()
    chunks = _png_chunks(payload)
    ihdr = next(data for kind, data in chunks if kind == b"IHDR")
    width, height, bit_depth, color_type, *_rest = struct.unpack(">IIBBBBB", ihdr)
    assert (width, height, bit_depth, color_type) == (1024, 1024, 8, 6)
    assert not {b"tEXt", b"zTXt", b"iTXt"} & {kind for kind, _data in chunks}

    width, height, rows = _rgba_rows(payload)
    corner_alpha = [
        rows[0][3],
        rows[0][(width - 1) * 4 + 3],
        rows[height - 1][3],
        rows[height - 1][(width - 1) * 4 + 3],
    ]
    assert corner_alpha == [0, 0, 0, 0]
    assert rows[height // 2][(width // 2) * 4 + 3] >= 250


def test_windows_icon_contains_every_required_png_size() -> None:
    entries = _ico_entries(ICO_PATH.read_bytes())
    assert {entry.width for entry in entries} == EXPECTED_ICON_SIZES
    assert all(entry.width == entry.height for entry in entries)
    assert all(entry.payload.startswith(PNG_SIGNATURE) for entry in entries)


def test_icon_builder_runs_under_windows_powershell(tmp_path: Path) -> None:
    output = tmp_path / "application.ico"
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BUILD_SCRIPT),
            "-Source",
            str(PNG_PATH),
            "-Output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert {entry.width for entry in _ico_entries(output.read_bytes())} == (
        EXPECTED_ICON_SIZES
    )
