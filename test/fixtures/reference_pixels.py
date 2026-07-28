from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from touptek4040.camera_contract import Frame, ReadoutMode, Roi


def reference_frame(
    *,
    readout_mode: ReadoutMode = ReadoutMode.RAW12,
) -> Frame:
    dtype = np.uint16 if readout_mode is ReadoutMode.RAW12 else np.uint8
    return Frame(
        pixels=np.array(
            [
                [1, 2, 3],
                [10, 20, 30],
            ],
            dtype=dtype,
        ),
        started_utc=datetime(
            2026,
            7,
            28,
            12,
            34,
            56,
            123000,
            tzinfo=timezone.utc,
        ),
        duration_s=0.125,
        binning=2,
        roi=Roi(7, 11, 3, 2),
        readout_mode=readout_mode,
        gain=321,
    )


def x_first_values() -> list[list[int]]:
    return [[1, 10], [2, 20], [3, 30]]
