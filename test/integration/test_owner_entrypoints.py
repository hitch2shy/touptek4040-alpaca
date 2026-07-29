"""Fresh Windows-spawn coverage for runtime camera-owner integration."""

from __future__ import annotations

from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import run_alpaca
from test.fixtures.fake_owner_backend import FakeOwnerBackend
from touptek4040.camera_contract import ReadoutMode, Roi
from touptek4040.camera_owner import OwnerStartupSpec
from touptek4040.camera_proxy import CameraOwnerProxy
from touptek4040.config import (
    AppConfig,
    CameraConfig,
    LoggingConfig,
    OwnerConfig,
    ServerConfig,
    WebConfig,
)
from touptek4040.errors import CameraError
from touptek4040.sim_camera import SimCamera


APPROVED_SERIAL = "TP241025143218C92B01151AEC40CB7"
PARENT_SDK_MODULES = {
    "touptek4040.toupcam_camera",
    "touptek4040.toupcam_loader",
    "_touptek4040_toupcam",
}


class _BlockingDisconnectBackend(FakeOwnerBackend):
    """Block only the connected Disconnect path until supervision kills us."""

    def disconnect(self) -> None:
        if not self.connected:
            return
        self._record("disconnect", outcome="blocked")
        time.sleep(60)


def create_blocking_disconnect_backend(
    serial: str | None = None,
    **kwargs: object,
) -> _BlockingDisconnectBackend:
    state_path = kwargs.get("state_path")
    return _BlockingDisconnectBackend(
        serial,
        state_path=str(state_path) if state_path is not None else None,
    )


def test_parent_entrypoint_import_does_not_load_sdk_loader_or_wrapper() -> None:
    command = (
        "import sys; import run_alpaca; "
        "assert 'touptek4040.toupcam_camera' not in sys.modules; "
        "assert 'touptek4040.toupcam_loader' not in sys.modules; "
        "assert '_touptek4040_toupcam' not in sys.modules"
    )

    completed = subprocess.run(
        [sys.executable, "-c", command],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr


def test_real_runtime_owner_and_heater_execute_only_in_spawned_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _active_child_pids()
    loaded_sdk_modules = PARENT_SDK_MODULES.intersection(sys.modules)
    state_path = tmp_path / "real-owner-state.json"
    proxies: list[CameraOwnerProxy] = []
    _install_spawned_fake_proxy(monkeypatch, proxies, state_path)
    coordinator, resolved = run_alpaca.build_coordinator(
        _config("toupcam", tmp_path)
    )

    try:
        assert resolved.camera.serial == APPROVED_SERIAL
        assert PARENT_SDK_MODULES.intersection(sys.modules) == loaded_sdk_modules
        coordinator.set_connected_sync(True)
        proxy = proxies[0]
        state = _read_state(state_path)
        owner_pid = int(state["backend_pid"])
        assert owner_pid != os.getpid()
        assert owner_pid in _active_child_pids() - baseline

        assert proxy.heater_level == 2
        proxy.heater_level = 3
        assert proxy.heater_level == 3
        state = _read_state(state_path)
        assert state["backend_pid"] == owner_pid
        assert state["heater_level"] == 3
        assert state["counts"]["get_heater_level"] == 4
        assert state["counts"]["set_heater_level"] == 1
        assert PARENT_SDK_MODULES.intersection(sys.modules) == loaded_sdk_modules
    finally:
        run_alpaca.close_camera_coordinator(coordinator)

    _wait_for_children(baseline)
    assert _registry_frames(tmp_path) == []


def test_simulator_runtime_and_heater_remain_in_parent_without_child(
    tmp_path: Path,
) -> None:
    baseline = _active_child_pids()
    coordinator, resolved = run_alpaca.build_coordinator(
        _config("sim", tmp_path)
    )

    coordinator.set_connected_sync(True)
    camera = coordinator._camera
    assert isinstance(camera, SimCamera)
    assert resolved.camera.source == "sim"
    assert _active_child_pids() == baseline
    assert camera.heater_level == 0
    camera.heater_level = 4
    assert camera.heater_level == 4
    assert _active_child_pids() == baseline
    run_alpaca.close_camera_coordinator(coordinator)

    assert _active_child_pids() == baseline
    assert not (tmp_path / "touptek4040-shared-frames.json").exists()


def test_runtime_shutdown_timeout_kills_exact_owner_and_leaves_no_frames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _active_child_pids()
    state_path = tmp_path / "blocking-owner-state.json"
    proxies: list[CameraOwnerProxy] = []
    _install_spawned_fake_proxy(
        monkeypatch,
        proxies,
        state_path,
        backend_module=__name__,
        backend_factory="create_blocking_disconnect_backend",
    )
    config = replace(
        _config("toupcam", tmp_path),
        owner=OwnerConfig(
            startup_timeout_s=2,
            connect_timeout_s=2,
            command_timeout_s=1,
            exposure_overhead_s=1,
            exposure_multiplier=1,
            abort_timeout_s=1,
            disconnect_timeout_s=0.1,
        ),
    )
    coordinator, resolved = run_alpaca.build_coordinator(config)
    server = run_alpaca.RuntimeServer(
        http=_HttpStub(),
        discovery=_DiscoveryStub(),
        coordinator=coordinator,
        config=resolved,
        start_discovery=False,
        logging_session=None,
    )
    coordinator.set_connected_sync(True)
    proxy = proxies[0]
    state = _read_state(state_path)
    exact_owner_pid = int(state["backend_pid"])
    frame = proxy.acquire(
        0.001,
        Roi(0, 0, 4, 4),
        1,
        ReadoutMode.RAW12,
    )
    assert frame.pixels.shape == (4, 4)
    assert _registry_frames(tmp_path) == []
    terminated_before_supervisor_close: list[int | None] = []
    original_close = proxy.close

    def record_and_close() -> None:
        terminated_before_supervisor_close.append(proxy.last_terminated_pid)
        original_close()

    proxy.close = record_and_close

    server.shutdown()

    assert terminated_before_supervisor_close == [exact_owner_pid]
    assert isinstance(coordinator.snapshot().error, CameraError)
    _wait_for_children(baseline)
    assert exact_owner_pid not in _active_child_pids()
    assert _registry_frames(tmp_path) == []


def _install_spawned_fake_proxy(
    monkeypatch: pytest.MonkeyPatch,
    proxies: list[CameraOwnerProxy],
    state_path: Path,
    *,
    backend_module: str = "test.fixtures.fake_owner_backend",
    backend_factory: str = "create_backend",
) -> None:
    def construct_proxy(
        *,
        serial: str,
        owner_config: OwnerConfig,
        runtime_directory: Path,
    ) -> CameraOwnerProxy:
        proxy = CameraOwnerProxy(
            owner_config=owner_config,
            runtime_directory=runtime_directory,
            startup_spec=OwnerStartupSpec(
                serial=serial,
                backend_module=backend_module,
                backend_factory=backend_factory,
                backend_arguments={"state_path": str(state_path)},
            ),
        )
        proxies.append(proxy)
        return proxy

    monkeypatch.setattr(run_alpaca, "CameraOwnerProxy", construct_proxy)


def _config(source: str, directory: Path) -> AppConfig:
    return AppConfig(
        server=ServerConfig("127.0.0.1", 11111, 32227),
        camera=CameraConfig(source, APPROVED_SERIAL),
        web=WebConfig(True),
        logging=LoggingConfig("INFO", directory),
        config_path=None,
    )


def _read_state(path: Path) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.01)
    raise AssertionError(f"owner state was not published: {path}")


def _registry_frames(directory: Path) -> list[object]:
    path = directory / "touptek4040-shared-frames.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["frames"]


def _active_child_pids() -> set[int]:
    return {
        int(process.pid)
        for process in multiprocessing.active_children()
        if process.pid is not None
    }


def _wait_for_children(expected: set[int]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _active_child_pids() == expected:
            return
        time.sleep(0.01)
    raise AssertionError(
        f"child process leak: expected {expected}, got {_active_child_pids()}"
    )


class _HttpStub:
    effective_port = 11111

    def __init__(self) -> None:
        self.task_dispatcher = type(
            "Dispatcher",
            (),
            {"shutdown": lambda self: None},
        )()

    def close(self) -> None:
        return None

    def run(self) -> None:
        return None


class _DiscoveryStub:
    def stop(self) -> None:
        return None
