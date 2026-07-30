"""Runnable ToupTek 4040 ASCOM Alpaca service."""

from __future__ import annotations

import argparse
import logging
import multiprocessing
import signal
import socket
import threading
import sys
from logging.handlers import RotatingFileHandler
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import Any

from waitress import create_server

from touptek4040.alpaca.app import create_app
from touptek4040.alpaca.discovery import DiscoveryResponder
from touptek4040.alpaca.management import SIMULATOR_SERIAL
from touptek4040.camera_coordinator import CameraCoordinator
from touptek4040.camera_proxy import CameraOwnerProxy
from touptek4040.config import AppConfig, load_config, runtime_base_dir
from touptek4040.errors import CameraInvalidValueError
from touptek4040.sim_camera import SimCamera

_LOG = logging.getLogger("touptek4040.runtime")
_LOGGING_LOCK = Lock()
_LOGGING_SESSION_COUNT = 0
_LOGGING_ORIGINAL_ROOT_LEVEL: int | None = None


def config_from_args(argv: Sequence[str] | None = None) -> AppConfig:
    """Parse command-line overrides without ever implicitly selecting simulation."""

    parser = argparse.ArgumentParser(description="ToupTek 4040 ASCOM Alpaca Camera")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--source", choices=("toupcam", "sim"))
    parser.add_argument("--serial")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--discovery-port", type=int)
    parser.add_argument("--log-level")
    parser.add_argument("--log-directory", type=Path)
    parser.add_argument("--no-web", action="store_true")
    args = parser.parse_args(argv)
    overrides = {
        "server.host": args.host,
        "server.port": args.port,
        "server.discovery_port": args.discovery_port,
        "camera.source": args.source,
        "camera.serial": args.serial,
        "logging.level": args.log_level,
        "logging.directory": args.log_directory,
        "web.enabled": False if args.no_web else None,
    }
    config_path = args.config or runtime_base_dir() / "touptek4040.json"
    config = load_config(config_path, overrides)
    if args.source == "sim" and args.serial is None:
        return replace(config, camera=replace(config.camera, serial=None))
    return config


class _OwnerProxyFactory:
    """Lazily create and retain the one proxy whose supervisor runtime owns."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._proxy: CameraOwnerProxy | None = None
        self._lock = Lock()

    def __call__(self, _source: str) -> CameraOwnerProxy:
        with self._lock:
            if self._proxy is None:
                self._proxy = CameraOwnerProxy(
                    serial=self._config.camera.serial,
                    owner_config=self._config.owner,
                    runtime_directory=self._config.logging.directory,
                )
            return self._proxy

    def close(self) -> None:
        with self._lock:
            proxy = self._proxy
        if proxy is not None:
            proxy.close()
            with self._lock:
                if self._proxy is proxy:
                    self._proxy = None


class RuntimeCameraCoordinator(CameraCoordinator):
    """Coordinator with an explicit runtime-owned proxy-supervisor boundary."""

    def __init__(self, factory, source: str, *, supervisor_close=None) -> None:
        super().__init__(factory, source)
        self._supervisor_close = supervisor_close

    def close_camera_supervisor(self) -> None:
        close = self._supervisor_close
        if close is not None:
            close()


def build_coordinator(config: AppConfig) -> tuple[CameraCoordinator, AppConfig]:
    """Resolve configured identity without touching the SDK in this process."""

    source = config.camera.source.strip().casefold()
    if source == "sim":
        resolved = replace(
            config,
            camera=replace(
                config.camera,
                source="sim",
                serial=SIMULATOR_SERIAL,
            ),
        )
        coordinator = RuntimeCameraCoordinator(
            lambda _source: SimCamera(SIMULATOR_SERIAL),
            "sim",
        )
        return coordinator, resolved
    if source != "toupcam":
        raise CameraInvalidValueError("camera source must be 'toupcam' or explicit 'sim'")

    configured_serial = config.camera.serial
    if not isinstance(configured_serial, str) or not configured_serial.strip():
        raise CameraInvalidValueError(
            "a non-empty camera serial is required for the real camera"
        )
    serial = configured_serial.strip()
    resolved = replace(
        config,
        camera=replace(config.camera, source="toupcam", serial=serial),
    )
    factory = _OwnerProxyFactory(resolved)
    return (
        RuntimeCameraCoordinator(
            factory,
            "toupcam",
            supervisor_close=factory.close,
        ),
        resolved,
    )


def close_camera_coordinator(coordinator: CameraCoordinator) -> None:
    """Disconnect first, then always close the retained owner supervisor."""

    disconnect_error: BaseException | None = None
    try:
        coordinator.set_connected_sync(False)
    except BaseException as error:
        disconnect_error = error
    try:
        close = getattr(coordinator, "close_camera_supervisor", None)
        if callable(close):
            close()
    except BaseException:
        if disconnect_error is None:
            raise
        _LOG.exception(
            "Camera owner supervisor close also failed after disconnect failure"
        )
    if disconnect_error is not None:
        raise disconnect_error


def build_server(
    config: AppConfig,
    *,
    start_discovery: bool = True,
) -> RuntimeServer:
    """Build the server only after real hardware identity is resolved."""

    logging_session = configure_logging(config)
    http = None
    try:
        coordinator, resolved = build_coordinator(config)
        app = create_app(coordinator, resolved)
        http = _create_owned_http(
            app,
            host=resolved.server.host,
            port=resolved.server.port,
            threads=8,
            clear_untrusted_proxy_headers=True,
        )
        effective_port, families = _http_listener_metadata(http)
        discovery = DiscoveryResponder(
            effective_port,
            resolved.server.discovery_port,
            families=families,
        )
        return RuntimeServer(
            http=http,
            discovery=discovery,
            coordinator=coordinator,
            config=resolved,
            start_discovery=start_discovery,
            logging_session=logging_session,
            port=effective_port,
        )
    except BaseException:
        if http is not None:
            _cleanup_bootstrap_http(http)
        logging_session.close()
        raise


class RuntimeServer:
    """Own HTTP, discovery and camera cleanup as one idempotent lifecycle."""

    def __init__(
        self,
        *,
        http: Any,
        discovery: DiscoveryResponder,
        coordinator: CameraCoordinator,
        config: AppConfig,
        start_discovery: bool,
        logging_session: LoggingSession | None,
        port: int | None = None,
    ) -> None:
        self.http = _OwnedWaitressServer.adopt(http)
        self.discovery = discovery
        self.coordinator = coordinator
        self.config = config
        self.port = (
            int(port)
            if port is not None
            else _http_listener_metadata(http)[0]
        )
        self._start_discovery = start_discovery
        self._shutdown_lock = Lock()
        self._logging_session = logging_session
        self.cleanup_error: BaseException | None = None
        self._cleaned = {
            "http": False,
            "discovery": False,
            "camera": False,
            "camera_owner": False,
            "logging": logging_session is None,
        }

    def serve_forever(self) -> None:
        if self._start_discovery:
            self.discovery.start()
        _LOG.info(
            "Alpaca server listening on %s:%s (%s)",
            self.config.server.host,
            self.port,
            self.config.camera.source,
        )
        self.http.run()

    def shutdown(self) -> None:
        with self._shutdown_lock:
            self._cleanup_step("http", self.http.close)
            self._cleanup_step("discovery", self.discovery.stop)
            self._cleanup_step(
                "camera",
                lambda: self.coordinator.set_connected_sync(False),
                terminal_on_failure=True,
            )
            self._cleanup_step(
                "camera_owner",
                lambda: _close_camera_supervisor(self.coordinator),
            )
            if all(
                self._cleaned[name]
                for name in ("http", "discovery", "camera_owner")
            ):
                _LOG.info("Alpaca server stopped")
            if self._logging_session is not None:
                self._cleanup_step("logging", self._logging_session.close)

    def _cleanup_step(
        self,
        name: str,
        action,
        *,
        terminal_on_failure: bool = False,
    ) -> None:
        if self._cleaned[name]:
            return
        try:
            action()
        except BaseException as error:
            if self.cleanup_error is None:
                self.cleanup_error = error
            if terminal_on_failure:
                self._cleaned[name] = True
            _LOG.exception("%s cleanup failed during shutdown", name)
        else:
            self._cleaned[name] = True


class LoggingSession:
    """One runtime-owned file handler with deterministic teardown."""

    def __init__(self, path: Path, handler: logging.Handler) -> None:
        self.path = path
        self.handler = handler
        self._closed = False

    def close(self) -> None:
        global _LOGGING_ORIGINAL_ROOT_LEVEL, _LOGGING_SESSION_COUNT
        if self._closed:
            return
        self._closed = True
        root = logging.getLogger()
        root.removeHandler(self.handler)
        self.handler.flush()
        self.handler.close()
        with _LOGGING_LOCK:
            _LOGGING_SESSION_COUNT -= 1
            if (
                _LOGGING_SESSION_COUNT == 0
                and _LOGGING_ORIGINAL_ROOT_LEVEL is not None
            ):
                root.setLevel(_LOGGING_ORIGINAL_ROOT_LEVEL)
                _LOGGING_ORIGINAL_ROOT_LEVEL = None


def configure_logging(config: AppConfig) -> LoggingSession:
    """Configure one bounded UTF-8 operational log owned by its runtime."""

    global _LOGGING_ORIGINAL_ROOT_LEVEL, _LOGGING_SESSION_COUNT
    directory = config.logging.directory
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "touptek4040.log"
    level_name = str(config.logging.level).upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        raise ValueError(f"invalid logging level {config.logging.level!r}")
    root = logging.getLogger()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    with _LOGGING_LOCK:
        if _LOGGING_SESSION_COUNT == 0:
            _LOGGING_ORIGINAL_ROOT_LEVEL = root.level
            root.setLevel(level)
        else:
            root.setLevel(min(root.level, level))
        _LOGGING_SESSION_COUNT += 1
        root.addHandler(file_handler)
    return LoggingSession(log_path, file_handler)


def _discovery_families(host: str) -> frozenset[int]:
    """Advertise only an address family on which HTTP is actually bound."""

    if ":" in host:
        return frozenset({socket.AF_INET6})
    try:
        families = frozenset(
            info[0]
            for info in socket.getaddrinfo(
                host,
                None,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
            if info[0] in {socket.AF_INET, socket.AF_INET6}
        )
    except OSError:
        families = frozenset()
    return families or frozenset({socket.AF_INET})


def _http_listener_metadata(http: Any) -> tuple[int, frozenset[int]]:
    listeners = getattr(http, "effective_listen", None)
    if not listeners:
        listeners = [
            (
                getattr(http, "effective_host", "0.0.0.0"),
                getattr(http, "effective_port"),
            )
        ]
    ports = {int(port) for _host, port in listeners}
    if len(ports) != 1:
        raise ValueError("all HTTP listeners must use the same port")
    families: set[int] = set()
    for host, _port in listeners:
        families.update(_discovery_families(str(host)))
    return ports.pop(), frozenset(families)


def _cleanup_bootstrap_http(http: Any) -> None:
    try:
        _OwnedWaitressServer.adopt(http).close()
    except BaseException:
        _LOG.exception("HTTP bootstrap cleanup failed")


def _contains_identity(values, candidate: Any) -> bool:
    return any(value is candidate for value in values)


def _unique_identities(values) -> list[Any]:
    unique: list[Any] = []
    for value in values:
        if not _contains_identity(unique, value):
            unique.append(value)
    return unique


def _remove_map_identity(socket_map: dict[Any, Any], entry: Any) -> None:
    for key, value in tuple(socket_map.items()):
        if value is entry:
            socket_map.pop(key, None)


def _close_waitress_map_once(
    socket_map: dict[Any, Any],
    closed_entries: list[Any] | None = None,
) -> list[BaseException]:
    """Close listener owners first, then only entries still left in the map."""

    closed = closed_entries if closed_entries is not None else []
    attempted: list[Any] = []
    errors: list[BaseException] = []

    def close_entry(entry: Any) -> None:
        if (
            _contains_identity(attempted, entry)
            or _contains_identity(closed, entry)
        ):
            return
        attempted.append(entry)
        try:
            entry.close()
        except BaseException as error:
            errors.append(error)
        else:
            closed.append(entry)
            _remove_map_identity(socket_map, entry)

    entries = _unique_identities(tuple(socket_map.values()))
    for entry in entries:
        if _contains_identity(entries, getattr(entry, "trigger", None)):
            close_entry(entry)
    for entry in _unique_identities(tuple(socket_map.values())):
        close_entry(entry)
    return errors


def _map_dispatchers(socket_map: dict[Any, Any]) -> list[Any]:
    return _unique_identities(
        dispatcher
        for entry in tuple(socket_map.values())
        if (dispatcher := getattr(entry, "task_dispatcher", None)) is not None
    )


class _OwnedWaitressServer:
    """Own Waitress's socket map and dispatcher as one close operation."""

    def __init__(
        self,
        server: Any,
        socket_map: dict[Any, Any] | None = None,
        dispatcher: Any | None = None,
    ) -> None:
        self._server = server
        self._socket_map = socket_map if socket_map is not None else {}
        self._dispatcher = (
            dispatcher
            if dispatcher is not None
            else getattr(server, "task_dispatcher", None)
        )
        self._standalone_server = not self._socket_map
        self._standalone_closed = False
        self._closed_entries: list[Any] = []
        self._dispatcher_closed = self._dispatcher is None
        self._close_lock = Lock()

    @classmethod
    def adopt(cls, server: Any) -> _OwnedWaitressServer:
        if isinstance(server, cls):
            return server
        socket_map = getattr(server, "map", None)
        return cls(
            server,
            socket_map if isinstance(socket_map, dict) else None,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._server, name)

    def run(self) -> None:
        self._server.run()

    def close(self) -> None:
        errors: list[BaseException] = []
        with self._close_lock:
            if self._standalone_server and not self._standalone_closed:
                try:
                    self._server.close()
                except BaseException as error:
                    errors.append(error)
                else:
                    self._standalone_closed = True
            else:
                errors.extend(
                    _close_waitress_map_once(
                        self._socket_map,
                        self._closed_entries,
                    )
                )
            if not self._dispatcher_closed:
                try:
                    self._dispatcher.shutdown()
                except BaseException as error:
                    errors.append(error)
                else:
                    self._dispatcher_closed = True
        if errors:
            for extra in errors[1:]:
                _LOG.error(
                    "Additional Waitress shutdown failure: %s",
                    extra,
                    exc_info=(
                        type(extra),
                        extra,
                        extra.__traceback__,
                    ),
                )
            raise errors[0]


def _create_owned_http(application: Any, **kwargs: Any) -> Any:
    """Construct every listener before starting Waitress worker threads."""

    socket_map: dict[Any, Any] = {}
    requested_threads = int(kwargs.pop("threads", 4))
    http = None
    try:
        http = create_server(
            application,
            map=socket_map,
            threads=0,
            **kwargs,
        )
        dispatcher = getattr(http, "task_dispatcher")
        dispatcher.set_thread_count(requested_threads)
        return _OwnedWaitressServer(http, socket_map, dispatcher)
    except BaseException:
        dispatchers = _map_dispatchers(socket_map)
        if http is not None:
            dispatcher = getattr(http, "task_dispatcher", None)
            if (
                dispatcher is not None
                and not _contains_identity(dispatchers, dispatcher)
            ):
                dispatchers.append(dispatcher)
        errors = _close_waitress_map_once(socket_map)
        if http is not None and not socket_map and not dispatchers:
            try:
                http.close()
            except BaseException as error:
                errors.append(error)
        for dispatcher in dispatchers:
            try:
                dispatcher.shutdown()
            except BaseException as error:
                errors.append(error)
        for error in errors:
            _LOG.error(
                "Partial Waitress construction cleanup failed: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
        raise


class ServiceTermination(Exception):
    """Unwind the service loop after a supported termination signal."""


def install_termination_handlers(termination: threading.Event):
    """Install supported service termination hooks and return a restorer."""

    previous: dict[int, Any] = {}

    def terminate(_signum, _frame) -> None:
        if termination.is_set():
            return
        termination.set()
        raise ServiceTermination

    supported = [signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        supported.append(signal.SIGBREAK)
    for number in supported:
        try:
            previous[number] = signal.getsignal(number)
            signal.signal(number, terminate)
        except (OSError, ValueError):
            previous.pop(number, None)

    def restore() -> None:
        for number, handler in previous.items():
            try:
                signal.signal(number, handler)
            except (OSError, ValueError):
                pass

    return restore


def _close_camera_supervisor(coordinator: CameraCoordinator) -> None:
    close = getattr(coordinator, "close_camera_supervisor", None)
    if callable(close):
        close()


def main(argv: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if effective_argv[:1] == ["--release-owner-probe"]:
        if len(effective_argv) != 2:
            raise SystemExit("--release-owner-probe requires exactly one result path")
        from touptek4040.release_probe import run_release_owner_probe

        return run_release_owner_probe(Path(effective_argv[1]))
    server = build_server(config_from_args(effective_argv))
    restore_handlers = lambda: None
    termination = threading.Event()
    try:
        restore_handlers = install_termination_handlers(termination)
        server.serve_forever()
        return 0
    except (KeyboardInterrupt, ServiceTermination):
        return 0
    finally:
        server.shutdown()
        restore_handlers()


if __name__ == "__main__":
    raise SystemExit(main())
