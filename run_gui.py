"""Launch the local, single-owner diagnostic application."""

from __future__ import annotations

from collections.abc import Sequence
import multiprocessing

from run_alpaca import config_from_args
from touptek4040.gui import run_diagnostic_gui


def main(argv: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()
    return run_diagnostic_gui(config_from_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
