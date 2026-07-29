from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import run_alpaca
import run_gui
from touptek4040.config import AppConfig, CameraConfig, LoggingConfig, ServerConfig, WebConfig
from touptek4040.errors import CameraNotConnectedError


def test_source_sim_must_be_explicit() -> None:
    config = run_alpaca.config_from_args(["--source", "sim"])

    assert config.camera.source == "sim"
    assert config.camera.serial is None


def test_production_defaults_to_real_camera() -> None:
    config = run_alpaca.config_from_args([])

    assert config.camera.source == "toupcam"


def test_real_bootstrap_resolves_serial_before_app_creation(monkeypatch) -> None:
    seen: list[str | None] = []
    identity = type("Identity", (), {"serial": "SERIAL-1"})()
    monkeypatch.setattr(run_alpaca, "enumerate_cameras", lambda: [identity])
    monkeypatch.setattr(
        run_alpaca,
        "create_app",
        lambda coordinator, config: seen.append(config.camera.serial) or object(),
    )
    monkeypatch.setattr(run_alpaca, "create_server", lambda *args, **kwargs: _WaitressStub())
    monkeypatch.setattr(run_alpaca, "DiscoveryResponder", _DiscoveryStub)

    server = run_alpaca.build_server(_config(source="toupcam"))

    assert seen == ["SERIAL-1"]
    assert server.config.camera.serial == "SERIAL-1"


def test_real_failure_never_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(
        run_alpaca,
        "enumerate_cameras",
        lambda: (_ for _ in ()).throw(CameraNotConnectedError("USB unavailable")),
    )

    with pytest.raises(CameraNotConnectedError, match="USB unavailable"):
        run_alpaca.build_server(_config(source="toupcam"))


def test_selected_serial_must_be_present(monkeypatch) -> None:
    identity = type("Identity", (), {"serial": "OTHER"})()
    monkeypatch.setattr(run_alpaca, "enumerate_cameras", lambda: [identity])

    with pytest.raises(CameraNotConnectedError, match="EXPECTED"):
        run_alpaca.build_server(_config(source="toupcam", serial="EXPECTED"))


def test_server_shutdown_stops_discovery_and_camera(monkeypatch) -> None:
    monkeypatch.setattr(run_alpaca, "create_server", lambda *args, **kwargs: _WaitressStub())
    monkeypatch.setattr(run_alpaca, "DiscoveryResponder", _DiscoveryStub)
    server = run_alpaca.build_server(_config(source="sim"))
    server.coordinator.set_connected_sync(True)

    server.shutdown()

    assert server.discovery.stopped
    assert server.http.closed
    assert not server.coordinator.snapshot().connected


def test_main_always_shuts_server_down(monkeypatch) -> None:
    stub = type(
        "RuntimeStub",
        (),
        {
            "served": False,
            "stopped": False,
            "serve_forever": lambda self: setattr(self, "served", True),
            "shutdown": lambda self: setattr(self, "stopped", True),
        },
    )()
    monkeypatch.setattr(run_alpaca, "build_server", lambda _config: stub)

    assert run_alpaca.main(["--source", "sim"]) == 0
    assert stub.served and stub.stopped


def test_gui_cli_delegates_to_diagnostic_application(monkeypatch) -> None:
    seen = {}
    def launch(config) -> int:
        seen["config"] = config
        return 0

    monkeypatch.setattr(
        run_gui,
        "run_diagnostic_gui",
        launch,
    )

    assert run_gui.main(["--source", "sim"]) == 0
    assert seen["config"].camera.source == "sim"


def _config(source: str, serial: str | None = None) -> AppConfig:
    return AppConfig(
        server=ServerConfig(host="127.0.0.1", port=11111, discovery_port=32227),
        camera=CameraConfig(source=source, serial=serial),
        web=WebConfig(enabled=True),
        logging=LoggingConfig(level="INFO", directory=Path("test/results/logs")),
        config_path=None,
    )


class _WaitressStub:
    effective_port = 11111

    def __init__(self) -> None:
        self.closed = False
        self.task_dispatcher = type("Dispatcher", (), {"shutdown": lambda self: None})()

    def run(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _DiscoveryStub:
    def __init__(self, *_args, **_kwargs) -> None:
        self.stopped = False

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.stopped = True
