"""Shared-port IPv4 and optional IPv6 Alpaca discovery responder."""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import threading
from collections.abc import Callable
from typing import Any

DISCOVERY_QUERY = b"alpacadiscovery1"
DISCOVERY_PORT = 32227
IPV6_MULTICAST_GROUP = "ff12::a1:9aca"
_MAX_DISCOVERY_DATAGRAM = 64

_LOG = logging.getLogger(__name__)


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
        logger: logging.Logger | None = None,
    ) -> None:
        _validate_port(alpaca_port, "alpaca_port")
        _validate_port(discovery_port, "discovery_port")
        if not callable(socket_factory):
            raise TypeError("socket_factory must be callable")
        self._payload = discovery_payload(alpaca_port)
        self._discovery_port = discovery_port
        self._socket_factory = socket_factory
        self._logger = logger or _LOG
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._sockets: list[Any] = []
        self._threads: list[threading.Thread] = []
        self._ipv6_warning_logged = False

    @property
    def running(self) -> bool:
        with self._lifecycle_lock:
            return any(thread.is_alive() for thread in self._threads)

    def start(self) -> None:
        """Start once; retain IPv4 even when optional IPv6 is unavailable."""

        with self._lifecycle_lock:
            if any(thread.is_alive() for thread in self._threads):
                return
            self._stop_event.clear()
            sockets: list[Any] = []
            try:
                sockets.append(self._open_ipv4())
            except BaseException:
                _close_all(sockets)
                raise
            try:
                sockets.append(self._open_ipv6())
            except BaseException as error:
                self._warn_ipv6_once(error)
            self._sockets = sockets
            self._threads = [
                threading.Thread(
                    target=self._serve_socket,
                    args=(sock,),
                    name=f"alpaca-discovery-{index}",
                    daemon=True,
                )
                for index, sock in enumerate(sockets, start=4)
            ]
            for thread in self._threads:
                thread.start()

    def stop(self) -> None:
        """Stop safely any number of times and release every owned socket."""

        with self._lifecycle_lock:
            self._stop_event.set()
            sockets = self._sockets
            threads = self._threads
            self._sockets = []
            self._threads = []
            _close_all(sockets)
        current = threading.current_thread()
        for thread in threads:
            if thread is not current:
                thread.join(timeout=2.0)

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
            membership = socket.inet_pton(
                socket.AF_INET6,
                IPV6_MULTICAST_GROUP,
            ) + struct.pack("@I", 0)
            sock.setsockopt(
                socket.IPPROTO_IPV6,
                socket.IPV6_JOIN_GROUP,
                membership,
            )
            sock.settimeout(0.2)
            return sock
        except BaseException:
            sock.close()
            raise

    def _serve_socket(self, sock: Any) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    payload, source = sock.recvfrom(
                        _MAX_DISCOVERY_DATAGRAM + 1
                    )
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    if not self._stop_event.is_set():
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
                    if not self._stop_event.is_set():
                        self._logger.warning(
                            "Could not send Alpaca discovery response "
                            "to request source"
                        )
        finally:
            try:
                sock.close()
            except OSError:
                pass

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


def _close_all(sockets: list[Any]) -> None:
    for sock in sockets:
        try:
            sock.close()
        except OSError:
            pass
