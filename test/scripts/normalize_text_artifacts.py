"""Normalize generated text evidence before hashing or retention."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def normalize_text_artifact(
    path: Path,
    *,
    replacements: Iterable[tuple[str, str]] = (),
) -> None:
    text = _decode_text(path.read_bytes())
    for old, new in replacements:
        text = text.replace(old, new)
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    normalized = "\n".join(lines) + "\n"
    path.write_bytes(normalized.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--replace",
        action="append",
        nargs=2,
        default=[],
        metavar=("OLD", "NEW"),
    )
    parser.add_argument("paths", nargs="+", type=Path)
    arguments = parser.parse_args(argv)
    replacements = tuple(
        (str(old), str(new)) for old, new in arguments.replace
    )
    for path in arguments.paths:
        normalize_text_artifact(path, replacements=replacements)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
