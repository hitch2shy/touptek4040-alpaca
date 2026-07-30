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
    return _registry_record_count(path) == 0


def _registry_record_count(path: Path) -> int:
    if not path.exists():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload in ({}, []):
        return 0
    if isinstance(payload, dict):
        records = payload.get("frames", payload.get("segments"))
        if isinstance(records, list):
            return len(records)
    raise AssertionError("shared-frame registry has an unexpected schema")


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
    frame_results = []
    try:
        proxy.connect()
        killed_pid = _owner_pid(proxy)
        with ThreadPoolExecutor(max_workers=1) as pool:
            def acquire_once() -> None:
                frame_results.append(
                    proxy.acquire(
                        1.0,
                        Roi(0, 0, 16, 16),
                        1,
                        ReadoutMode.RAW12,
                    )
                )

            pending = pool.submit(acquire_once)
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
        acquire_request_count = proxy._supervisor.acquire_request_count
        start_ack_count = proxy._supervisor.acquire_start_ack_count
        frame_descriptor_count = proxy._supervisor.frame_descriptor_count
        frame_result_count = len(frame_results)
        registry_record_count = _registry_record_count(registry)
        owner_owned_frame_count = int(diagnostic_payload["owned_frame_count"])
        observed_publication_count = max(
            frame_descriptor_count,
            frame_result_count,
            registry_record_count,
            owner_owned_frame_count,
        )
        native_start_acknowledged = start_ack_count == 1
        exposure_retried = acquire_request_count > 1
        frame_published = observed_publication_count > 0
        assert acquire_request_count == 1
        assert start_ack_count == 1
        assert frame_descriptor_count == 0
        assert frame_result_count == 0
        assert registry_record_count == 0
        assert owner_owned_frame_count == 0
        assert observed_publication_count == 0

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
            native_start_acknowledged=native_start_acknowledged,
            acquire_request_count=acquire_request_count,
            start_ack_count=start_ack_count,
            frame_descriptor_count=frame_descriptor_count,
            frame_result_count=frame_result_count,
            registry_record_count=registry_record_count,
            owner_owned_frame_count=owner_owned_frame_count,
            observed_publication_count=observed_publication_count,
            exposure_retried=exposure_retried,
            frame_published=frame_published,
            explicit_reconnect_required=True,
            shared_frame_registry_empty=registry_record_count == 0,
        )
    finally:
        proxy.close()
        restoration.restore_and_close()

    assert killed_pid is not None
    assert SharedFrameRegistry._pid_is_dead(killed_pid)
    assert _registry_is_empty(registry)
