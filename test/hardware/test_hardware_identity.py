from __future__ import annotations

import os
from pathlib import Path

import pytest

from touptek4040.toupcam_camera import enumerate_cameras
from touptek4040.toupcam_loader import load_toupcam_module


pytestmark = pytest.mark.skipif(
    os.environ.get("TOUPTEK_HARDWARE_TEST") != "1",
    reason="set TOUPTEK_HARDWARE_TEST=1 to access attached hardware",
)


def test_connected_camera_has_approved_identity() -> None:
    sdk = load_toupcam_module(Path(__file__).resolve().parents[2])

    identities = enumerate_cameras(sdk)

    assert len(identities) == 1
    identity = identities[0]
    assert identity.model == "sMAX16AM-U3-CL-GPS"
    assert identity.serial == "TP241025143218C92B01151AEC40CB7"
    assert identity.capabilities.width == 4096
    assert identity.capabilities.height == 4096
    assert identity.capabilities.pixel_size_x == 9.0
    assert identity.capabilities.pixel_size_y == 9.0
