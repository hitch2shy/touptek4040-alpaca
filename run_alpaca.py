"""Runnable ToupTek 4040 ASCOM Alpaca service."""

from __future__ import annotations

import argparse
import logging
import signal
import socket
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
from touptek4040.config import AppConfig, load_config
from touptek4040.errors import CameraError, CameraInvalidValueError, CameraNotConnectedError
from touptek4040.sim_camera import SimCamera
from touptek4040.toupcam_camera import ToupTekCamera, enumerate_cameras

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
    return load_config(args.config, overrides)


def build_coordinator(config: AppConfig) -> tuple[CameraCoordinator, AppConfig]:
    """Resolve stable identity, then create the single camera lifecycle owner."""

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
        coordinator = CameraCoordinator(
            lambda _source: SimCamera(SIMULATOR_SERIAL),
            "sim",
        )
        return coordinator, resolved
    if source != "toupcam":
        raise CameraInvalidValueError("camera source must be 'toupcam' or explicit 'sim'")

    identities = enumerate_cameras()
    configured_serial = config.camera.serial
    if configured_serial:
        selected = next(
            (identity for identity in identities if identity.serial == configured_serial),
            None,
        )
        if selected is None:
            raise CameraNotConnectedError(
                f"camera with serial {configured_serial!r} is not attached"
            )
    elif len(identities) == 1:
        selected = identities[0]
    elif not identities:
        raise CameraNotConnectedError("no ToupTek camera is attached")
    else:
        raise CameraInvalidValueError(
            "multiple ToupTek cameras are attached; select one with --serial"
        )
    serial = str(selected.serial).strip()
    if not serial:
        raise CameraNotConnectedError("attached ToupTek camera has no stable serial")
    resolved = replace(
        config,
        camera=replace(config.camera, source="toupcam", serial=serial),
    )
    return (
        CameraCoordinator(lambda _source: ToupTekCamera(serial), "toupcam"),
        resolved,
    )


def build_server(
    config: AppConfig,
    *,
    start_discovery: bool = True,
) -> RuntimeServer:
    """Build the server only after real hardware identity is resolved."""

    logging_session = configure_logging(config)
    try:
        coordinator, resolved = build_coordinator(config)
        app = create_app(coordinator, resolved)
        http = create_server(
            app,
            host=resolved.server.host,
            port=resolved.server.port,
            threads=8,
            clear_untrusted_proxy_headers=True,
        )
        discovery = DiscoveryResponder(
            int(http.effective_port),
            resolved.server.discovery_port,
            families=_discovery_families(resolved.server.host),
        )
        return RuntimeServer(
            http=http,
            discovery=discovery,
            coordinator=coordinator,
            config=resolved,
            start_discovery=start_discovery,
            logging_session=logging_session,
        )
    except BaseException:
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
    ) -> None:
        self.http = http
        self.discovery = discovery
        self.coordinator = coordinator
        self.config = config
        self.port = int(http.effective_port)
        self._start_discovery = start_discovery
        self._shutdown_lock = Lock()
        self._logging_session = logging_session
        self._cleaned = {
            "http": False,
            "dispatcher": False,
            "discovery": False,
            "camera": False,
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
            dispatcher = getattr(self.http, "task_dispatcher", None)
            self._cleanup_step(
                "dispatcher",
                (dispatcher.shutdown if dispatcher is not None else lambda: None),
            )
            self._cleanup_step("discovery", self.discovery.stop)
            self._cleanup_step(
                "camera",
                lambda: self.coordinator.set_connected_sync(False),
            )
            if all(
                self._cleaned[name]
                for name in ("http", "dispatcher", "discovery", "camera")
            ):
                _LOG.info("Alpaca server stopped")
            if self._logging_session is not None:
                self._cleanup_step("logging", self._logging_session.close)

    def _cleanup_step(self, name: str, action) -> None:
        if self._cleaned[name]:
            return
        try:
            action()
        except BaseException:
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


def install_termination_handlers(server: RuntimeServer):
    """Install supported service termination hooks and return a restorer."""

    previous: dict[int, Any] = {}

    def terminate(_signum, _frame) -> None:
        server.shutdown()

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


def main(argv: Sequence[str] | None = None) -> int:
    server = build_server(config_from_args(argv))
    restore_handlers = install_termination_handlers(server)
    try:
        server.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        server.shutdown()
        restore_handlers()


if __name__ == "__main__":
    raise SystemExit(main())
