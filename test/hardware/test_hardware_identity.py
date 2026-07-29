from __future__ import annotations

import os
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("TOUPTEK_HARDWARE_TEST") != "1",
    reason="set TOUPTEK_HARDWARE_TEST=1 to access attached hardware",
)


def test_connected_camera_has_approved_identity(hardware_session) -> None:
    # The session fixture already performed the one allowed read-only
    # enumeration before opening its exclusive acquisition handle.
    identity = hardware_session.identity
    assert identity.model == "sMAX16AM-U3-CL-GPS"
    assert identity.serial == "TP241025143218C92B01151AEC40CB7"
    assert identity.capabilities.width == 4096
    assert identity.capabilities.height == 4096
    assert identity.capabilities.pixel_size_x == 9.0
    assert identity.capabilities.pixel_size_y == 9.0
