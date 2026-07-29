"""Shared-port IPv4 and optional IPv6 Alpaca discovery responder."""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DISCOVERY_QUERY = b"alpacadiscovery1"
DISCOVERY_PORT = 32227
IPV6_MULTICAST_GROUP = "ff12::a1:9aca"
_MAX_DISCOVERY_DATAGRAM = 64

_LOG = logging.getLogger(__name__)


@dataclass
class _DiscoveryRun:
    generation: int
    stop_event: threading.Event
    sockets: dict[int, Any]
    threads: dict[int, threading.Thread]


def discovery_payload(alpaca_port: int) -> bytes:
    """Return the exact compact ASCII discovery response."""

    _validate_port(alpaca_port, "alpaca_port")
    return json.dumps(
        {"AlpacaPort": alpaca_port},
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def is_discovery_query(payload: object) -> bool:
    """Accept only the current exact 16-byte ASCII discovery message."""

    return isinstance(payload, bytes) and payload == DISCOVERY_QUERY


class DiscoveryResponder:
    """Idempotent lifecycle owner for Alpaca UDP discovery sockets."""

    def __init__(
        self,
        alpaca_port: int,
        discovery_port: int = DISCOVERY_PORT,
        *,
        socket_factory: Callable[[int, int], Any] = socket.socket,
        interface_enumerator: Callable[
            [], list[tuple[int, str]]
        ] = socket.if_nameindex,
        logger: logging.Logger | None = None,
    ) -> None:
        _validate_port(alpaca_port, "alpaca_port")
        _validate_port(discovery_port, "discovery_port")
        if not callable(socket_factory):
            raise TypeError("socket_factory must be callable")
        if not callable(interface_enumerator):
            raise TypeError("interface_enumerator must be callable")
        self._payload = discovery_payload(alpaca_port)
        self._discovery_port = discovery_port
        self._socket_factory = socket_factory
        self._interface_enumerator = interface_enumerator
        self._logger = logger or _LOG
        self._operation_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._generation = 0
        self._run: _DiscoveryRun | None = None
        self._ipv6_warning_logged = False

    @property
    def running(self) -> bool:
        with self._lifecycle_lock:
            run = self._run
            if run is None or run.stop_event.is_set():
                return False
            ipv4_thread = run.threads.get(socket.AF_INET)
            return ipv4_thread is not None and ipv4_thread.is_alive()

    def start(self) -> None:
        """Start once; retain IPv4 even when optional IPv6 is unavailable."""

        with self._operation_lock:
            with self._lifecycle_lock:
                current = self._run
                if self._run_has_healthy_ipv4(current):
                    return
                self._run = None
            self._stop_and_join(current)

            sockets: dict[int, Any] = {}
            try:
                sockets[socket.AF_INET] = self._open_ipv4()
            except BaseException:
                _close_all(sockets.values())
                raise
            try:
                sockets[socket.AF_INET6] = self._open_ipv6()
            except BaseException as error:
                self._warn_ipv6_once(error)

            stop_event = threading.Event()
            self._generation += 1
            generation = self._generation
            threads = {
                family: threading.Thread(
                    target=self._serve_socket,
                    args=(sock, family, generation, stop_event),
                    name=(
                        "alpaca-discovery-ipv4"
                        if family == socket.AF_INET
                        else "alpaca-discovery-ipv6"
                    ),
                    daemon=True,
                )
                for family, sock in sockets.items()
            }
            run = _DiscoveryRun(
                generation,
                stop_event,
                sockets,
                threads,
            )
            with self._lifecycle_lock:
                self._run = run
            started: list[threading.Thread] = []
            try:
                for thread in threads.values():
                    thread.start()
                    started.append(thread)
            except BaseException:
                with self._lifecycle_lock:
                    if self._run is run:
                        self._run = None
                run.stop_event.set()
                _close_all(run.sockets.values())
                _join_threads(started)
                raise

    def stop(self) -> None:
        """Stop safely any number of times and release every owned socket."""

        with self._operation_lock:
            with self._lifecycle_lock:
                run = self._run
                self._run = None
            self._stop_and_join(run)

    @staticmethod
    def _run_has_healthy_ipv4(run: _DiscoveryRun | None) -> bool:
        if run is None or run.stop_event.is_set():
            return False
        thread = run.threads.get(socket.AF_INET)
        return thread is not None and thread.is_alive()

    @staticmethod
    def _stop_and_join(run: _DiscoveryRun | None) -> None:
        if run is None:
            return
        run.stop_event.set()
        _close_all(run.sockets.values())
        _join_threads(run.threads.values())

    def _listener_failed(self, generation: int, family: int) -> None:
        with self._lifecycle_lock:
            run = self._run
            if run is None or run.generation != generation:
                return
            if family == socket.AF_INET:
                run.stop_event.set()
                _close_all(run.sockets.values())
            else:
                ipv6_socket = run.sockets.get(socket.AF_INET6)
                if ipv6_socket is not None:
                    _close_all((ipv6_socket,))

    def _open_ipv4(self) -> Any:
        sock = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            _set_shared_options(sock)
            sock.bind(("", self._discovery_port))
            sock.settimeout(0.2)
            return sock
        except BaseException:
            sock.close()
            raise

    def _open_ipv6(self) -> Any:
        sock = self._socket_factory(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            _set_shared_options(sock)
            if hasattr(socket, "IPV6_V6ONLY"):
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind(("::", self._discovery_port))
            group = socket.inet_pton(socket.AF_INET6, IPV6_MULTICAST_GROUP)
            joined = 0
            rejected = 0
            seen_indexes: set[int] = set()
            for index, _name in self._interface_enumerator():
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or not 1 <= index <= 0xFFFFFFFF
                    or index in seen_indexes
                ):
                    continue
                seen_indexes.add(index)
                try:
                    sock.setsockopt(
                        socket.IPPROTO_IPV6,
                        socket.IPV6_JOIN_GROUP,
                        group + struct.pack("@I", index),
                    )
                    joined += 1
                except OSError:
                    rejected += 1
            if joined == 0:
                raise OSError(
                    "no IPv6 interface could join Alpaca discovery multicast"
                )
            if rejected:
                self._logger.debug(
                    "IPv6 Alpaca discovery joined %s interface(s); "
                    "%s inapplicable interface(s) were skipped",
                    joined,
                    rejected,
                )
            sock.settimeout(0.2)
            return sock
        except BaseException:
            sock.close()
            raise

    def _serve_socket(
        self,
        sock: Any,
        family: int,
        generation: int,
        stop_event: threading.Event,
    ) -> None:
        failed = False
        try:
            while not stop_event.is_set():
                try:
                    payload, source = sock.recvfrom(
                        _MAX_DISCOVERY_DATAGRAM + 1
                    )
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    if not stop_event.is_set():
                        failed = True
                        self._logger.exception(
                            "Alpaca discovery socket stopped unexpectedly"
                        )
                    return
                except BaseException:
                    self._logger.exception(
                        "Alpaca discovery receive failed; listener remains active"
                    )
                    continue
                if not is_discovery_query(payload):
                    continue
                try:
                    sock.sendto(self._payload, source)
                except OSError:
                    if not stop_event.is_set():
                        self._logger.warning(
                            "Could not send Alpaca discovery response "
                            "to request source"
                        )
        finally:
            try:
                sock.close()
            except OSError:
                pass
            if failed:
                self._listener_failed(generation, family)

    def _warn_ipv6_once(self, error: BaseException) -> None:
        if self._ipv6_warning_logged:
            return
        self._ipv6_warning_logged = True
        self._logger.warning(
            "IPv6 Alpaca discovery is unavailable; IPv4 discovery remains active. "
            "Enable an IPv6 interface and multicast support to use %s. (%s)",
            IPV6_MULTICAST_GROUP,
            type(error).__name__,
        )


def _set_shared_options(sock: Any) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if os.name != "nt" and hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)


def _validate_port(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError(f"{name} must be an integer from 1 through 65535")


def _close_all(sockets: Any) -> None:
    for sock in sockets:
        try:
            sock.close()
        except OSError:
            pass


def _join_threads(threads: Any) -> None:
    current = threading.current_thread()
    for thread in threads:
        if thread is not current:
            thread.join(timeout=2.0)
