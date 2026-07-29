from __future__ import annotations

import logging
import signal
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_alpaca
import run_gui
from touptek4040.alpaca.management import SIMULATOR_SERIAL
from touptek4040.camera_contract import ReadoutMode
from touptek4040.config import AppConfig, CameraConfig, LoggingConfig, ServerConfig, WebConfig
from touptek4040.errors import CameraInvalidValueError, CameraTimeoutError
from touptek4040.gui import (
    GuiCloseController,
    DiagnosticWorkerPool,
    QueueLogHandler,
    _cleanup_coordinator,
    parse_diagnostic_settings,
    run_diagnostic_gui,
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


def test_real_runtime_constructs_lazy_owner_proxy_from_resolved_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []

    class ProxyStub:
        def connect(self) -> None:
            events.append("connect")

        def disconnect(self) -> None:
            events.append("disconnect")

        def abort(self) -> None:
            events.append("abort")

        def close(self) -> None:
            events.append("close")

    def construct_proxy(**kwargs):
        events.append(kwargs)
        return ProxyStub()

    monkeypatch.setattr(run_alpaca, "CameraOwnerProxy", construct_proxy)
    config = replace(
        _config(source="toupcam", serial=" SERIAL-1 "),
        logging=LoggingConfig("INFO", tmp_path),
    )

    coordinator, resolved = run_alpaca.build_coordinator(config)

    assert events == []
    assert resolved.camera.serial == "SERIAL-1"
    coordinator.set_connected_sync(True)
    assert events[0] == {
        "serial": "SERIAL-1",
        "owner_config": resolved.owner,
        "runtime_directory": tmp_path,
    }
    run_alpaca.close_camera_coordinator(coordinator)
    assert events[1:] == ["connect", "abort", "disconnect", "close"]


def test_owner_proxy_close_failure_retains_exact_supervisor_for_retry(
    monkeypatch,
) -> None:
    close_calls: list[int] = []

    class ProxyStub:
        def connect(self) -> None:
            return None

        def disconnect(self) -> None:
            return None

        def abort(self) -> None:
            return None

        def close(self) -> None:
            close_calls.append(len(close_calls) + 1)
            if len(close_calls) == 1:
                raise RuntimeError("exact owner still alive")

    monkeypatch.setattr(
        run_alpaca,
        "CameraOwnerProxy",
        lambda **_kwargs: ProxyStub(),
    )
    coordinator, _resolved = run_alpaca.build_coordinator(
        _config(source="toupcam", serial="SERIAL-1")
    )
    coordinator.set_connected_sync(True)
    coordinator.set_connected_sync(False)

    with pytest.raises(RuntimeError, match="still alive"):
        coordinator.close_camera_supervisor()
    coordinator.close_camera_supervisor()

    assert close_calls == [1, 2]


@pytest.mark.parametrize("serial", [None, "", "   "])
def test_real_runtime_requires_a_configured_serial(serial) -> None:
    with pytest.raises(CameraInvalidValueError, match="camera serial"):
        run_alpaca.build_coordinator(_config(source="toupcam", serial=serial))


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
    assert events.index("camera-disconnect") < events.index("camera-owner-close")


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


def test_disconnect_timeout_then_owner_close_is_terminal_and_error_stable() -> None:
    timeout = CameraTimeoutError("stable disconnect timeout")
    coordinator = _FailingCleanupCoordinator(disconnect_error=timeout)
    server = run_alpaca.RuntimeServer(
        http=_WaitressStub(),
        discovery=_DiscoveryStub(),
        coordinator=coordinator,
        config=_config("sim"),
        start_discovery=False,
        logging_session=None,
    )

    server.shutdown()
    server.shutdown()

    assert coordinator.events == ["camera-disconnect", "camera-owner-close"]
    assert coordinator.current_error is timeout
    assert server.cleanup_error is timeout


def test_owner_close_failure_retries_exact_retained_supervisor_only() -> None:
    timeout = CameraTimeoutError("stable disconnect timeout")
    close_error = RuntimeError("exact supervisor still alive")
    coordinator = _FailingCleanupCoordinator(
        disconnect_error=timeout,
        close_errors=[close_error],
    )
    server = run_alpaca.RuntimeServer(
        http=_WaitressStub(),
        discovery=_DiscoveryStub(),
        coordinator=coordinator,
        config=_config("sim"),
        start_discovery=False,
        logging_session=None,
    )

    server.shutdown()
    server.shutdown()

    assert coordinator.events == [
        "camera-disconnect",
        "camera-owner-close",
        "camera-owner-close",
    ]
    assert coordinator.close_targets == [coordinator.proxy, coordinator.proxy]
    assert coordinator.current_error is timeout
    assert server.cleanup_error is timeout


def test_repeated_successful_runtime_shutdown_is_idempotent() -> None:
    coordinator = _FailingCleanupCoordinator()
    server = run_alpaca.RuntimeServer(
        http=_WaitressStub(),
        discovery=_DiscoveryStub(),
        coordinator=coordinator,
        config=_config("sim"),
        start_discovery=False,
        logging_session=None,
    )

    server.shutdown()
    server.shutdown()

    assert coordinator.events == ["camera-disconnect", "camera-owner-close"]
    assert server.cleanup_error is None


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
    with pytest.raises(CameraInvalidValueError, match="camera serial"):
        run_alpaca.build_server(_config("toupcam"))
    assert session.closed


def test_main_always_shuts_server_down(monkeypatch) -> None:
    events: list[str] = []
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
    monkeypatch.setattr(
        run_alpaca.multiprocessing,
        "freeze_support",
        lambda: events.append("freeze"),
    )
    monkeypatch.setattr(
        run_alpaca,
        "config_from_args",
        lambda _argv: events.append("config") or _config("sim"),
    )
    monkeypatch.setattr(
        run_alpaca,
        "build_server",
        lambda _config: events.append("server") or stub,
    )

    assert run_alpaca.main(["--source", "sim"]) == 0
    assert stub.served and stub.stopped
    assert events == ["freeze", "config", "server"]


def test_signal_handler_uses_same_shutdown_path(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(signal, "getsignal", lambda number: f"old-{number}")
    monkeypatch.setattr(signal, "signal", lambda number, handler: calls.append((number, handler)))
    termination = threading.Event()
    restore = run_alpaca.install_termination_handlers(termination)
    installed = dict(calls)
    with pytest.raises(run_alpaca.ServiceTermination):
        installed[signal.SIGTERM](signal.SIGTERM, None)
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert termination.is_set()
    restore()
    assert any(handler == f"old-{number}" for number, handler in calls)


def test_discovery_families_follow_http_bind(monkeypatch) -> None:
    seen = []
    def server_for_bind(*_args, **kwargs):
        server = _WaitressStub()
        server.effective_host = kwargs["host"]
        return server
    monkeypatch.setattr(run_alpaca, "create_server", server_for_bind)
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
    closer = threading.Thread(target=lambda: pool.close(disconnect).join(1))
    closer.start()
    release.set()
    closer.join(1)
    assert cleanup and all(item == "disconnect" for item in cleanup)
    assert camera_open == [False]
    assert not pool.submit(lambda: None, lambda *_: None)


def test_accepted_but_unscheduled_worker_is_cancelled_by_close() -> None:
    delayed = []
    action_ran = []
    cleanup = []

    class DelayedThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target
            delayed.append(self)

        def start(self):
            return None

        def join(self, _timeout=None):
            return None

    pool = DiagnosticWorkerPool(thread_factory=DelayedThread)
    assert pool.submit(lambda: action_ran.append(True), lambda *_: None)
    finalizer = pool.close(lambda: cleanup.append(True))
    finalizer.join(1)
    delayed[0].target()
    assert action_ran == []
    assert cleanup == [True]


@pytest.mark.parametrize("error", [KeyboardInterrupt(), RuntimeError("Tk failed")])
def test_mainloop_failure_always_runs_cleanup(error) -> None:
    root = type("Root", (), {"mainloop": lambda self: (_ for _ in ()).throw(error)})()
    cleaned = []
    with pytest.raises(type(error)):
        run_mainloop_with_cleanup(root, lambda: cleaned.append(True))
    assert cleaned == [True]


def test_multisocket_listener_metadata_drives_discovery(monkeypatch) -> None:
    seen = []
    http = _MultiSocketStub([("127.0.0.1", 23456), ("::1", 23456)])
    monkeypatch.setattr(run_alpaca, "create_server", lambda *args, **kwargs: http)
    monkeypatch.setattr(
        run_alpaca,
        "DiscoveryResponder",
        lambda port, _discovery_port, **kwargs: (
            seen.append((port, kwargs["families"])) or _DiscoveryStub()
        ),
    )
    server = run_alpaca.build_server(_config("sim"))
    assert server.port == 23456
    assert seen == [
        (
            23456,
            frozenset({run_alpaca.socket.AF_INET, run_alpaca.socket.AF_INET6}),
        )
    ]
    server.shutdown()


def test_multisocket_mismatched_ports_fail_and_cleanup(monkeypatch) -> None:
    http = _MultiSocketStub([("127.0.0.1", 11111), ("::1", 22222)])
    monkeypatch.setattr(run_alpaca, "create_server", lambda *args, **kwargs: http)
    with pytest.raises(ValueError, match="same port"):
        run_alpaca.build_server(_config("sim"))
    assert http.closed and http.drained


def test_discovery_bootstrap_failure_closes_http(monkeypatch) -> None:
    http = _MultiSocketStub([("127.0.0.1", 11111)])
    monkeypatch.setattr(run_alpaca, "create_server", lambda *args, **kwargs: http)
    monkeypatch.setattr(
        run_alpaca,
        "DiscoveryResponder",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("discovery failed")),
    )
    with pytest.raises(OSError, match="discovery failed"):
        run_alpaca.build_server(_config("sim"))
    assert http.closed and http.drained


def test_ipv6_only_discovery_run_is_reported_running() -> None:
    responder = run_alpaca.DiscoveryResponder(
        11111, families=frozenset({run_alpaca.socket.AF_INET6})
    )
    responder._run = SimpleNamespace(
        stop_event=threading.Event(),
        threads={
            run_alpaca.socket.AF_INET6: SimpleNamespace(is_alive=lambda: True)
        },
    )
    assert responder.running is True


def test_gui_partial_widget_construction_cleans_all_resources(monkeypatch) -> None:
    session = SimpleNamespace(closed=False)
    session.close = lambda: setattr(session, "closed", True)
    disconnects = []
    coordinator = SimpleNamespace(
        preview_active=False,
        abort_exposure=lambda: None,
        set_connected_sync=lambda value: disconnects.append(value),
        close_camera_supervisor=lambda: disconnects.append("close"),
    )
    monkeypatch.setattr(run_alpaca, "configure_logging", lambda _config: session)
    monkeypatch.setattr(
        run_alpaca,
        "build_coordinator",
        lambda config: (coordinator, config),
    )

    class Root:
        def title(self, _value): pass
        def minsize(self, *_value): pass

    class Tk:
        Tk = Root
        StringVar = lambda *args, **kwargs: SimpleNamespace(
            get=lambda: kwargs.get("value", ""),
            set=lambda _value: None,
        )
        BooleanVar = StringVar
        Text = object

    class Ttk:
        @staticmethod
        def Frame(*_args, **_kwargs):
            raise RuntimeError("widget construction failed")

    messagebox = SimpleNamespace(showerror=lambda *args, **kwargs: None)
    logger = logging.getLogger("touptek4040")
    before = tuple(logger.handlers)
    with pytest.raises(RuntimeError, match="widget construction failed"):
        run_diagnostic_gui(
            _config("sim"),
            _ui_modules=(Tk, messagebox, Ttk),
        )
    assert session.closed
    assert disconnects == [False, "close"]
    assert tuple(logger.handlers) == before


@pytest.mark.parametrize("cleanup_raises", [False, True])
def test_gui_close_waits_for_final_cleanup_before_destroy(cleanup_raises) -> None:
    release = threading.Event()
    action_started = threading.Event()
    cleanup_finished = threading.Event()
    root = _PollingRoot()
    buttons = SimpleNamespace(
        winfo_children=lambda: [
            SimpleNamespace(configure=lambda **_kwargs: None)
        ]
    )
    pool = DiagnosticWorkerPool()
    assert pool.submit(
        lambda: (action_started.set(), release.wait()),
        lambda *_: None,
    )
    assert action_started.wait(1)

    def cleanup() -> None:
        cleanup_finished.set()
        if cleanup_raises:
            raise RuntimeError("cleanup failed")

    controller = GuiCloseController(root, buttons, pool, cleanup)
    controller.request_close()
    assert root.destroyed is False
    root.run_one_callback()
    assert root.destroyed is False
    release.set()
    controller.join()
    root.run_all_callbacks()
    assert cleanup_finished.is_set()
    assert root.destroyed is True
    assert bool(controller.cleanup_error) is cleanup_raises


def test_gui_cleanup_retries_same_supervisor_once_and_completes_exit() -> None:
    first_error = RuntimeError("first exact close failed")
    coordinator = _GuiCleanupCoordinator([first_error])
    root = _PollingRoot()
    controller = GuiCloseController(
        root,
        SimpleNamespace(winfo_children=lambda: []),
        DiagnosticWorkerPool(),
        lambda: _cleanup_coordinator(coordinator),
    )

    controller.request_close()
    controller.join()
    controller.destroy_root()

    assert coordinator.events == [
        "abort",
        "disconnect",
        "close",
        "close",
    ]
    assert coordinator.close_targets == [coordinator.proxy, coordinator.proxy]
    assert controller.cleanup_error is None
    assert root.destroyed is True


def test_gui_cleanup_records_first_error_after_two_bounded_close_failures(
    caplog,
) -> None:
    first_error = RuntimeError("first exact close failed")
    second_error = RuntimeError("second exact close failed")
    coordinator = _GuiCleanupCoordinator([first_error, second_error])
    root = _PollingRoot()
    controller = GuiCloseController(
        root,
        SimpleNamespace(winfo_children=lambda: []),
        DiagnosticWorkerPool(),
        lambda: _cleanup_coordinator(coordinator),
    )

    controller.request_close()
    controller.join()
    controller.destroy_root()

    assert coordinator.events == [
        "abort",
        "disconnect",
        "close",
        "close",
    ]
    assert coordinator.close_targets == [coordinator.proxy, coordinator.proxy]
    assert controller.cleanup_error is first_error
    assert root.destroyed is True
    assert "first exact close failed" in caplog.text


def test_gui_button_failure_still_joins_cleanup_before_destroy() -> None:
    events: list[str] = []
    root = _PollingRoot(events)
    buttons = SimpleNamespace(
        winfo_children=lambda: [
            SimpleNamespace(
                configure=lambda **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("button is broken")
                )
            )
        ]
    )
    controller = GuiCloseController(
        root,
        buttons,
        DiagnosticWorkerPool(),
        lambda: events.append("camera-cleanup"),
    )

    with pytest.raises(RuntimeError, match="button is broken"):
        controller.request_close()
    controller.join()
    controller.destroy_root()
    events.append("logging-close")

    assert events == ["camera-cleanup", "root-destroy", "logging-close"]
    assert root.destroy_threads == [threading.get_ident()]
    assert controller.finalizer is not None
    assert not controller.finalizer.is_alive()


def test_gui_mainloop_failure_joins_cleanup_and_destroys_on_tk_thread() -> None:
    events: list[str] = []
    root = _PollingRoot(events, mainloop_error=RuntimeError("Tk failed"))
    buttons = SimpleNamespace(winfo_children=lambda: [])
    controller = GuiCloseController(
        root,
        buttons,
        DiagnosticWorkerPool(),
        lambda: events.append("camera-cleanup"),
    )

    def final_cleanup() -> None:
        controller.request_close()
        controller.join()
        controller.destroy_root()
        events.append("logging-close")

    with pytest.raises(RuntimeError, match="Tk failed"):
        run_mainloop_with_cleanup(root, final_cleanup)

    assert events == ["camera-cleanup", "root-destroy", "logging-close"]
    assert root.destroy_threads == [threading.get_ident()]
    assert controller.finalizer is not None
    assert not controller.finalizer.is_alive()


def test_waitress_partial_multisocket_creation_closes_each_owned_resource_once(
    monkeypatch,
) -> None:
    events: list[str] = []
    dispatcher = _CountingDispatcher(events)

    def partial_failure(_app, *, map, threads, **kwargs):
        assert threads == 0
        assert "_dispatcher" not in kwargs
        _add_owned_listener(map, 10, "first", dispatcher, events)
        map[20] = _MapEntry(map, 20, "second-trigger", events)
        raise OSError("second bind failed")

    monkeypatch.setattr(run_alpaca, "create_server", partial_failure)
    with pytest.raises(OSError, match="second bind failed"):
        run_alpaca._create_owned_http(
            object(), host="localhost", port=11111, threads=8
        )

    assert events == [
        "first-server-close",
        "first-trigger-close",
        "second-trigger-close",
        "dispatcher-shutdown",
    ]
    assert dispatcher.started == []
    assert dispatcher.shutdown_count == 1


def test_waitress_multisocket_runtime_has_one_shutdown_owner(monkeypatch) -> None:
    events: list[str] = []
    dispatcher = _CountingDispatcher(events)
    returned = SimpleNamespace(
        effective_listen=[("127.0.0.1", 11111), ("::1", 11111)],
        task_dispatcher=dispatcher,
        run=lambda: None,
        close=lambda: events.append("returned-multisocket-close"),
    )

    def successful_create(_app, *, map, threads, **kwargs):
        assert threads == 0
        assert "_dispatcher" not in kwargs
        _add_owned_listener(map, 10, "first", dispatcher, events)
        _add_owned_listener(map, 20, "second", dispatcher, events)
        return returned

    monkeypatch.setattr(run_alpaca, "create_server", successful_create)
    http = run_alpaca._create_owned_http(
        object(), host="localhost", port=11111, threads=8
    )
    server = run_alpaca.RuntimeServer(
        http=http,
        discovery=_DiscoveryStub(events),
        coordinator=_CoordinatorStub(events),
        config=_config("sim"),
        start_discovery=False,
        logging_session=None,
    )

    server.shutdown()
    server.shutdown()

    assert dispatcher.started == [8]
    assert dispatcher.shutdown_count == 1
    assert "returned-multisocket-close" not in events
    for name in ("first-server", "first-trigger", "second-server", "second-trigger"):
        assert events.count(f"{name}-close") == 1


def test_gui_cli_delegates_to_diagnostic_application(monkeypatch) -> None:
    seen = {}
    events: list[str] = []
    def launch(config) -> int:
        events.append("gui")
        seen["config"] = config
        return 0

    monkeypatch.setattr(
        run_gui.multiprocessing,
        "freeze_support",
        lambda: events.append("freeze"),
    )
    monkeypatch.setattr(
        run_gui,
        "config_from_args",
        lambda _argv: events.append("config") or _config("sim"),
    )
    monkeypatch.setattr(
        run_gui,
        "run_diagnostic_gui",
        launch,
    )

    assert run_gui.main(["--source", "sim"]) == 0
    assert seen["config"].camera.source == "sim"
    assert events == ["freeze", "config", "gui"]


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
            "Dispatcher",
            (),
            {
                "set_thread_count": lambda self, _count: None,
                "shutdown": lambda self: events_ref.append("http-drain"),
            },
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

    def close_camera_supervisor(self) -> None:
        self.events.append("camera-owner-close")


class _FailingCleanupCoordinator:
    def __init__(
        self,
        *,
        disconnect_error: BaseException | None = None,
        close_errors: list[BaseException] | None = None,
    ) -> None:
        self.disconnect_error = disconnect_error
        self.close_errors = list(close_errors or [])
        self.events: list[str] = []
        self.proxy = object()
        self.close_targets: list[object] = []
        self.current_error: BaseException | None = None

    def set_connected_sync(self, connected: bool) -> None:
        assert connected is False
        self.events.append("camera-disconnect")
        if self.current_error is not None:
            self.current_error = RuntimeError("original cleanup error overwritten")
            raise self.current_error
        if self.disconnect_error is not None:
            self.current_error = self.disconnect_error
            raise self.disconnect_error

    def close_camera_supervisor(self) -> None:
        self.events.append("camera-owner-close")
        self.close_targets.append(self.proxy)
        if self.close_errors:
            raise self.close_errors.pop(0)


class _GuiCleanupCoordinator:
    preview_active = False

    def __init__(self, close_errors: list[BaseException]) -> None:
        self.close_errors = list(close_errors)
        self.events: list[str] = []
        self.proxy = object()
        self.close_targets: list[object] = []

    def stop_preview(self) -> None:
        raise AssertionError("preview is inactive")

    def abort_exposure(self) -> None:
        self.events.append("abort")

    def set_connected_sync(self, connected: bool) -> None:
        assert connected is False
        self.events.append("disconnect")

    def close_camera_supervisor(self) -> None:
        self.events.append("close")
        self.close_targets.append(self.proxy)
        if self.close_errors:
            raise self.close_errors.pop(0)


class _MultiSocketStub:
    def __init__(self, effective_listen) -> None:
        self.effective_listen = effective_listen
        self.closed = False
        self.drained = False
        self.task_dispatcher = SimpleNamespace(
            set_thread_count=lambda _count: None,
            shutdown=lambda: setattr(self, "drained", True)
        )

    def close(self) -> None:
        self.closed = True

    def run(self) -> None:
        return None


class _MapEntry:
    def __init__(self, socket_map, key: int, name: str, events: list[str]) -> None:
        self.socket_map = socket_map
        self.key = key
        self.name = name
        self.events = events
        # Real Waitress trigger channels also expose a non-channel ``trigger``
        # write handle; only a server's trigger points back into the map.
        self.trigger = object()

    def close(self) -> None:
        self.events.append(f"{self.name}-close")
        self.socket_map.pop(self.key, None)


class _OwningMapEntry(_MapEntry):
    def __init__(
        self,
        socket_map,
        key: int,
        name: str,
        events: list[str],
        trigger: _MapEntry,
        dispatcher,
    ) -> None:
        super().__init__(socket_map, key, name, events)
        self.trigger = trigger
        self.task_dispatcher = dispatcher

    def close(self) -> None:
        super().close()
        self.trigger.close()


class _CountingDispatcher:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.started: list[int] = []
        self.shutdown_count = 0

    def set_thread_count(self, count: int) -> None:
        self.started.append(count)

    def shutdown(self) -> None:
        self.shutdown_count += 1
        self.events.append("dispatcher-shutdown")


def _add_owned_listener(
    socket_map,
    key: int,
    name: str,
    dispatcher,
    events: list[str],
) -> None:
    trigger = _MapEntry(socket_map, key, f"{name}-trigger", events)
    server = _OwningMapEntry(
        socket_map,
        key + 1,
        f"{name}-server",
        events,
        trigger,
        dispatcher,
    )
    socket_map[key] = trigger
    socket_map[key + 1] = server


class _PollingRoot:
    def __init__(self, events=None, mainloop_error=None) -> None:
        self.callbacks = []
        self.destroyed = False
        self.destroy_threads = []
        self.events = events if events is not None else []
        self.mainloop_error = mainloop_error

    def mainloop(self) -> None:
        if self.mainloop_error is not None:
            raise self.mainloop_error

    def after(self, _delay, callback) -> None:
        self.callbacks.append(callback)

    def destroy(self) -> None:
        self.events.append("root-destroy")
        self.destroy_threads.append(threading.get_ident())
        self.destroyed = True

    def run_one_callback(self) -> None:
        callback = self.callbacks.pop(0)
        callback()

    def run_all_callbacks(self) -> None:
        while self.callbacks and not self.destroyed:
            self.run_one_callback()
