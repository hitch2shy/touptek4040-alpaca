from __future__ import annotations

import logging
import signal
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import run_alpaca
import run_gui
from touptek4040.alpaca.management import SIMULATOR_SERIAL
from touptek4040.camera_contract import ReadoutMode
from touptek4040.config import AppConfig, CameraConfig, LoggingConfig, ServerConfig, WebConfig
from touptek4040.errors import CameraNotConnectedError
from touptek4040.gui import (
    DiagnosticWorkerPool,
    QueueLogHandler,
    parse_diagnostic_settings,
    run_mainloop_with_cleanup,
)


def test_source_sim_must_be_explicit() -> None:
    config = run_alpaca.config_from_args(["--source", "sim"])

    assert config.camera.source == "sim"
    assert config.camera.serial is None


def test_sim_runtime_normalizes_fixed_identity_even_with_hardware_serial() -> None:
    coordinator, config = run_alpaca.build_coordinator(
        _config(source="sim", serial="HARDWARE-SERIAL")
    )

    assert config.camera.serial == SIMULATOR_SERIAL
    coordinator.set_connected_sync(True)
    assert coordinator.snapshot().connected
    coordinator.set_connected_sync(False)


@pytest.mark.parametrize(
    ("display", "expected"),
    [("RAW 12-bit", ReadoutMode.RAW12), ("RAW 8-bit", ReadoutMode.RAW8)],
)
def test_gui_parses_both_raw_display_modes(display, expected) -> None:
    settings = parse_diagnostic_settings(
        duration="0.1", gain="100", binning="1", mode=display,
        roi_x="0", roi_y="0", roi_width="32", roi_height="24",
    )
    assert settings.mode is expected


def test_gui_reports_stable_invalid_field_error() -> None:
    with pytest.raises(ValueError, match="Check the camera setting fields"):
        parse_diagnostic_settings(
            duration="bad", gain="100", binning="1", mode="RAW 12-bit",
            roi_x="0", roi_y="0", roi_width="32", roi_height="24",
        )


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
    server.shutdown()


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


def test_server_stops_and_drains_http_before_camera_disconnect(monkeypatch) -> None:
    events: list[str] = []
    http = _WaitressStub(events)
    coordinator = _CoordinatorStub(events)
    server = run_alpaca.RuntimeServer(
        http=http,
        discovery=_DiscoveryStub(events),
        coordinator=coordinator,
        config=_config("sim"),
        start_discovery=False,
        logging_session=None,
    )
    server.shutdown()
    assert events.index("http-close") < events.index("http-drain") < events.index("camera-disconnect")


def test_shutdown_attempts_all_cleanup_and_retries_only_failed_step() -> None:
    events: list[str] = []
    http = _WaitressStub(events)
    http.fail_close_once = True
    coordinator = _CoordinatorStub(events)
    discovery = _DiscoveryStub(events)
    server = run_alpaca.RuntimeServer(
        http=http,
        discovery=discovery,
        coordinator=coordinator,
        config=_config("sim"),
        start_discovery=False,
        logging_session=None,
    )
    server.shutdown()
    assert "http-drain" in events and "camera-disconnect" in events and "discovery-stop" in events
    server.shutdown()
    assert events.count("http-close") == 2
    assert events.count("camera-disconnect") == 1


def test_shutdown_drains_inflight_connected_put_before_disconnect() -> None:
    events: list[str] = []
    release_request = threading.Event()
    drain_started = threading.Event()
    http = _WaitressStub(events)
    def drain() -> None:
        events.append("http-drain")
        drain_started.set()
        assert release_request.wait(1)
    http.task_dispatcher.shutdown = drain
    server = run_alpaca.RuntimeServer(
        http=http,
        discovery=_DiscoveryStub(events),
        coordinator=_CoordinatorStub(events),
        config=_config("sim"),
        start_discovery=False,
        logging_session=None,
    )
    shutdown = threading.Thread(target=server.shutdown)
    shutdown.start()
    assert drain_started.wait(1)
    assert "camera-disconnect" not in events
    release_request.set()
    shutdown.join(1)
    assert events.index("http-drain") < events.index("camera-disconnect")


def test_bootstrap_failure_closes_runtime_logging(monkeypatch) -> None:
    session = type("Session", (), {"closed": False, "close": lambda self: setattr(self, "closed", True)})()
    monkeypatch.setattr(run_alpaca, "configure_logging", lambda _config: session)
    monkeypatch.setattr(
        run_alpaca,
        "enumerate_cameras",
        lambda: (_ for _ in ()).throw(CameraNotConnectedError("missing")),
    )
    with pytest.raises(CameraNotConnectedError):
        run_alpaca.build_server(_config("toupcam"))
    assert session.closed


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


def test_signal_handler_uses_same_shutdown_path(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(signal, "getsignal", lambda number: f"old-{number}")
    monkeypatch.setattr(signal, "signal", lambda number, handler: calls.append((number, handler)))
    server = type("Server", (), {"shutdown": lambda self: setattr(self, "stopped", True)})()
    restore = run_alpaca.install_termination_handlers(server)
    installed = dict(calls)
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert server.stopped
    restore()
    assert any(handler == f"old-{number}" for number, handler in calls)


def test_discovery_families_follow_http_bind(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(run_alpaca, "create_server", lambda *args, **kwargs: _WaitressStub())
    monkeypatch.setattr(
        run_alpaca,
        "DiscoveryResponder",
        lambda *args, **kwargs: seen.append(kwargs["families"]) or _DiscoveryStub(),
    )
    first = run_alpaca.build_server(_config("sim"))
    second = run_alpaca.build_server(
        replace(_config("sim"), server=ServerConfig("::", 11111, 32227))
    )
    assert seen == [frozenset({run_alpaca.socket.AF_INET}), frozenset({run_alpaca.socket.AF_INET6})]
    first.shutdown()
    second.shutdown()


def test_discovery_families_support_dual_stack_hostname(monkeypatch) -> None:
    monkeypatch.setattr(
        run_alpaca.socket,
        "getaddrinfo",
        lambda *args: [
            (run_alpaca.socket.AF_INET, 0, 0, "", ()),
            (run_alpaca.socket.AF_INET6, 0, 0, "", ()),
        ],
    )
    assert run_alpaca._discovery_families("camera-host") == frozenset(
        {run_alpaca.socket.AF_INET, run_alpaca.socket.AF_INET6}
    )


def test_logging_sessions_use_requested_destination_and_close(tmp_path: Path) -> None:
    original_level = logging.getLogger().level
    first = run_alpaca.configure_logging(
        replace(_config("sim"), logging=LoggingConfig("INFO", tmp_path / "one"))
    )
    second = run_alpaca.configure_logging(
        replace(_config("sim"), logging=LoggingConfig("INFO", tmp_path / "two"))
    )
    logging.getLogger("touptek4040.test").warning("separate destinations")
    first.close()
    second.close()
    assert first.path.parent.name == "one"
    assert second.path.parent.name == "two"
    assert first.handler not in logging.getLogger().handlers
    assert second.handler not in logging.getLogger().handlers
    assert logging.getLogger().level == original_level


def test_queue_log_handler_has_no_leak() -> None:
    received = []
    handler = QueueLogHandler(received.append)
    logger = logging.getLogger("touptek4040.gui-test")
    logger.addHandler(handler)
    logger.warning("SDK warning")
    logger.removeHandler(handler)
    handler.close()
    logger.warning("after close")
    assert len(received) == 1 and "SDK warning" in received[0]


def test_worker_pool_close_cleans_up_after_queued_connect() -> None:
    connected = threading.Event()
    release = threading.Event()
    cleanup = []
    camera_open = [False]
    pool = DiagnosticWorkerPool()
    def connect() -> None:
        connected.set()
        release.wait()
        camera_open[0] = True
    def disconnect() -> None:
        camera_open[0] = False
        cleanup.append("disconnect")
    assert pool.submit(connect, lambda *_: None)
    assert connected.wait(1)
    closer = threading.Thread(target=lambda: pool.close(disconnect, timeout=0.05))
    closer.start()
    release.set()
    closer.join(1)
    assert cleanup and all(item == "disconnect" for item in cleanup)
    assert camera_open == [False]
    assert not pool.submit(lambda: None, lambda *_: None)


@pytest.mark.parametrize("error", [KeyboardInterrupt(), RuntimeError("Tk failed")])
def test_mainloop_failure_always_runs_cleanup(error) -> None:
    root = type("Root", (), {"mainloop": lambda self: (_ for _ in ()).throw(error)})()
    cleaned = []
    with pytest.raises(type(error)):
        run_mainloop_with_cleanup(root, lambda: cleaned.append(True))
    assert cleaned == [True]


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

    def __init__(self, events=None) -> None:
        self.closed = False
        self.events = events if events is not None else []
        self.fail_close_once = False
        events_ref = self.events
        self.task_dispatcher = type(
            "Dispatcher", (), {"shutdown": lambda self: events_ref.append("http-drain")}
        )()

    def run(self) -> None:
        return None

    def close(self) -> None:
        self.events.append("http-close")
        if self.fail_close_once:
            self.fail_close_once = False
            raise OSError("close failed")
        self.closed = True


class _DiscoveryStub:
    def __init__(self, events=None, *_args, **_kwargs) -> None:
        self.stopped = False
        self.events = events if isinstance(events, list) else []

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.events.append("discovery-stop")
        self.stopped = True


class _CoordinatorStub:
    def __init__(self, events) -> None:
        self.events = events

    def set_connected_sync(self, connected: bool) -> None:
        assert connected is False
        self.events.append("camera-disconnect")
