from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time

import pytest

from test.hardware.conftest import (
    EXPECTED_SERIAL,
    HardwareSession,
    open_verified_target,
)
from touptek4040.camera_contract import ReadoutMode, Roi
from touptek4040.camera_owner import OwnerStartupSpec
from touptek4040.camera_proxy import CameraOwnerProxy
from touptek4040.config import OwnerConfig
from touptek4040.errors import CameraTimeoutError
from touptek4040.shared_frames import SharedFrameRegistry
from touptek4040.toupcam_loader import load_toupcam_module


def _owner_pid(proxy: CameraOwnerProxy) -> int:
    session = proxy._supervisor._session
    assert session is not None
    pid = session.process.pid
    assert isinstance(pid, int) and pid > 0
    return pid


def _registry_is_empty(path: Path) -> bool:
    if not path.exists():
        return True
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload in ({}, [], {"segments": []}, {"frames": []})


@pytest.mark.hardware
def test_real_owner_termination_does_not_retry_or_publish_and_requires_reconnect(
    tmp_path,
    hardware_evidence,
) -> None:
    """Let the configured deadline kill one post-start held acquisition."""

    sdk = load_toupcam_module(Path(__file__).resolve().parents[2])
    identity = open_verified_target(sdk, hardware_evidence)
    restoration = HardwareSession(sdk, identity, hardware_evidence)
    restoration.connect_and_snapshot()
    restoration.camera.disconnect()

    registry = tmp_path / "touptek4040-shared-frames.json"
    proxy = CameraOwnerProxy(
        serial=EXPECTED_SERIAL,
        runtime_directory=tmp_path,
        owner_config=OwnerConfig(
            startup_timeout_s=10.0,
            connect_timeout_s=20.0,
            command_timeout_s=5.0,
            exposure_overhead_s=0.3,
            exposure_multiplier=1.02,
            abort_timeout_s=2.0,
            disconnect_timeout_s=10.0,
        ),
        startup_spec=OwnerStartupSpec(
            serial=EXPECTED_SERIAL,
            backend_arguments={"post_acquire_start_hold_s": 2.0},
        ),
    )
    killed_pid = None
    original_generation = proxy.owner_generation
    try:
        proxy.connect()
        killed_pid = _owner_pid(proxy)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                proxy.acquire,
                1.0,
                Roi(0, 0, 16, 16),
                1,
                ReadoutMode.RAW12,
            )
            deadline = time.monotonic() + 2.0
            while (
                proxy._supervisor.last_acquire_start_ack is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.002)
            ack = proxy._supervisor.last_acquire_start_ack
            assert ack is not None
            assert ack[0] == original_generation
            assert ack[2] == pytest.approx(1.0)
            assert proxy._supervisor.acquire_start_ack_count == 1
            assert proxy.exposing is True

            with pytest.raises(CameraTimeoutError):
                pending.result(timeout=5.0)

        assert proxy.connected is False
        assert proxy.owner_generation != original_generation
        assert proxy.last_terminated_pid == killed_pid
        replacement_session, diagnostics = proxy._supervisor.request(
            "diagnostics", {}, require_connected=False
        )
        diagnostic_payload = proxy._supervisor.validate(
            replacement_session,
            lambda: diagnostics.result,
        )
        assert isinstance(diagnostic_payload, dict)
        assert diagnostic_payload["connected"] is False
        assert _registry_is_empty(registry)

        # A replacement generation must remain disconnected until this call.
        proxy.connect()
        assert proxy.connected is True
        assert proxy.capabilities.width == 4096
        assert proxy.capabilities.height == 4096
        proxy.disconnect()
        hardware_evidence.record(
            "owner-termination",
            terminated_pid=killed_pid,
            terminated_pid_alive=not SharedFrameRegistry._pid_is_dead(killed_pid),
            original_generation=original_generation,
            replacement_generation=proxy.owner_generation,
            native_start_acknowledged=True,
            acquire_operation_count=proxy._supervisor.acquire_start_ack_count,
            exposure_retried=False,
            frame_published=False,
            explicit_reconnect_required=True,
            shared_frame_registry_empty=_registry_is_empty(registry),
        )
    finally:
        proxy.close()
        restoration.restore_and_close()

    assert killed_pid is not None
    assert SharedFrameRegistry._pid_is_dead(killed_pid)
    assert _registry_is_empty(registry)
