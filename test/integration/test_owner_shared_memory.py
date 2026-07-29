"""Crash-point evidence for owner-published shared-memory frames."""

from __future__ import annotations

import json
import multiprocessing
from multiprocessing import shared_memory
import os
from pathlib import Path
import time
from typing import Any

import pytest

from test.integration.test_owner_hangs import (
    _ObservedConnection,
    _record_result,
)
from touptek4040.camera_contract import ReadoutMode, Roi
from touptek4040.camera_owner import OwnerStartupSpec, camera_owner_main
from touptek4040.camera_proxy import CameraOwnerProxy, OwnerProcessSession
from touptek4040.config import OwnerConfig
from touptek4040.errors import CameraError


_CRASH_POINT = ""
_CRASH_EVENTS: Any = None
_CRASH_CLAIMED: Any = None
_LAST_DESCRIPTOR_NAME: str | None = None


def _claim_crash() -> bool:
    with _CRASH_CLAIMED.get_lock():
        if _CRASH_CLAIMED.value:
            return False
        _CRASH_CLAIMED.value = 1
        return True


def _publish_event(crash_point: str, name: str | None = None) -> None:
    _CRASH_EVENTS.put(
        {
            "crash_point": crash_point,
            "pid": os.getpid(),
            "name": name,
        }
    )
    time.sleep(0.03)


class _CrashSendConnection(_ObservedConnection):
    def send(self, payload: object) -> None:
        global _LAST_DESCRIPTOR_NAME
        if type(payload) is dict:
            operation = payload.get("operation")
            result = payload.get("result")
            if operation == "acquire" and type(result) is dict:
                descriptor = result.get("descriptor")
                if type(descriptor) is dict:
                    name = descriptor.get("name")
                    if isinstance(name, str):
                        _LAST_DESCRIPTOR_NAME = name
                if (
                    _CRASH_POINT == "after_descriptor_send"
                    and _claim_crash()
                ):
                    super().send(payload)
                    _publish_event(_CRASH_POINT, _LAST_DESCRIPTOR_NAME)
                    os._exit(72)
            if (
                operation == "release_frame"
                and _CRASH_POINT == "after_proxy_copy_before_release_ack"
                and _claim_crash()
            ):
                _publish_event(_CRASH_POINT, _LAST_DESCRIPTOR_NAME)
                os._exit(73)
        super().send(payload)


def _crashing_owner_main(
    command_connection: Any,
    control_connection: Any,
    startup_spec: OwnerStartupSpec,
    generation: int,
    registry_path: str,
    crash_point: str,
    crash_events: Any,
    crash_claimed: Any,
) -> None:
    global _CRASH_POINT, _CRASH_EVENTS, _CRASH_CLAIMED
    _CRASH_POINT = crash_point
    _CRASH_EVENTS = crash_events
    _CRASH_CLAIMED = crash_claimed
    import touptek4040.camera_owner as owner_module

    original_registry = owner_module.SharedFrameRegistry

    class CrashRegistry(original_registry):
        def publish(self, pixels: Any, metadata: Any):
            if _CRASH_POINT == "before_creation" and _claim_crash():
                _publish_event(_CRASH_POINT)
                os._exit(70)
            descriptor = super().publish(pixels, metadata)
            if _CRASH_POINT == "after_creation" and _claim_crash():
                _publish_event(_CRASH_POINT, descriptor.name)
                os._exit(71)
            return descriptor

    owner_module.SharedFrameRegistry = CrashRegistry
    camera_owner_main(
        _CrashSendConnection(
            command_connection, default_generation=generation
        ),
        _ObservedConnection(
            control_connection,
            control=True,
            default_generation=generation,
        ),
        startup_spec,
        generation,
        registry_path,
    )


class _CrashProcessFactory:
    def __init__(self, crash_point: str) -> None:
        self.context = multiprocessing.get_context("spawn")
        self.crash_point = crash_point
        self.events = self.context.Queue()
        self.claimed = self.context.Value("b", 0)

    def start(
        self,
        startup_spec: OwnerStartupSpec,
        generation: int,
        registry_path: Path,
    ) -> OwnerProcessSession:
        command_parent, command_child = self.context.Pipe(duplex=True)
        control_parent, control_child = self.context.Pipe(duplex=True)
        process = self.context.Process(
            target=_crashing_owner_main,
            args=(
                command_child,
                control_child,
                startup_spec,
                generation,
                str(registry_path),
                self.crash_point,
                self.events,
                self.claimed,
            ),
        )
        process.start()
        command_child.close()
        control_child.close()
        return OwnerProcessSession(process, command_parent, control_parent)


def run_shared_memory_crash(
    crash_point: str, tmp_path: Path
) -> dict[str, object]:
    registry_path = tmp_path / "touptek4040-shared-frames.json"
    factory = _CrashProcessFactory(crash_point)
    proxy = CameraOwnerProxy(
        owner_config=OwnerConfig(
            startup_timeout_s=0.6,
            connect_timeout_s=0.3,
            command_timeout_s=0.3,
            exposure_overhead_s=0.3,
            exposure_multiplier=1.0,
            abort_timeout_s=0.3,
            disconnect_timeout_s=0.3,
        ),
        runtime_directory=tmp_path,
        startup_spec=OwnerStartupSpec(
            serial="FAULT",
            backend_module="test.integration.test_owner_hangs",
            backend_factory="create_hanging_backend",
        ),
        process_factory=factory,
    )
    owner_generation = proxy.owner_generation
    try:
        proxy.connect()
        with pytest.raises(CameraError):
            proxy.acquire(
                0.001,
                Roi(0, 0, 4, 4),
                1,
                ReadoutMode.RAW12,
            )
        event = factory.events.get(timeout=1)
        owner_pid = int(event["pid"])
        names = [event["name"]] if isinstance(event.get("name"), str) else []
        registry_names = _registry_names(registry_path)
        live_names = [name for name in names if _segment_exists(name)]
        assert proxy.owner_generation != owner_generation
        assert proxy.connected is False
        return {
            "registry_names": registry_names,
            "live_segment_names": live_names,
            "owner_pid_alive": _pid_alive(owner_pid),
        }
    finally:
        proxy.close()


def _registry_names(path: Path) -> list[str]:
    if not path.exists():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    return [str(item["name"]) for item in document["frames"]]


def _segment_exists(name: str) -> bool:
    try:
        mapping = shared_memory.SharedMemory(name=name)
    except FileNotFoundError:
        return False
    mapping.close()
    return True


def _pid_alive(pid: int) -> bool:
    from touptek4040.shared_frames import SharedFrameRegistry

    return not SharedFrameRegistry._pid_is_dead(pid)


@pytest.mark.parametrize(
    "crash_point",
    [
        "before_creation",
        "after_creation",
        "after_descriptor_send",
        "after_proxy_copy_before_release_ack",
    ],
)
def test_owner_frame_crash_leaves_no_registered_or_live_segment(
    crash_point: str,
    tmp_path: Path,
) -> None:
    result = run_shared_memory_crash(crash_point, tmp_path)

    assert result["registry_names"] == []
    assert result["live_segment_names"] == []
    assert result["owner_pid_alive"] is False
    _record_result(
        {"kind": "shared-memory", "name": crash_point, **result}
    )
