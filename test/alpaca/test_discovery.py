from __future__ import annotations

import importlib
import json
import logging
import socket
import threading
import time

import pytest


def _discovery_module():
    try:
        return importlib.import_module("touptek4040.alpaca.discovery")
    except ModuleNotFoundError:
        pytest.fail("Discovery responder module is not implemented")


def test_discovery_payload_has_exact_compact_ascii_key_and_validates_port():
    module = _discovery_module()

    assert module.discovery_payload(11111) == b'{"AlpacaPort":11111}'
    assert json.loads(module.discovery_payload(11111)) == {"AlpacaPort": 11111}
    for invalid in (True, 0, -1, 65536, "11111", None):
        with pytest.raises(ValueError, match="alpaca_port"):
            module.discovery_payload(invalid)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"alpacadiscovery",
        b"ALPACADISCOVERY1",
        b"alpacadiscovery2",
        b" alpacadiscovery1",
        b"alpacadiscovery1 ",
        b"alpacadiscovery1client-field",
        b"alpacadiscovery1" + (b"x" * 1_024),
        b"\xff\xfealpacadiscovery1",
    ],
)
def test_only_exact_ascii_v1_query_is_accepted(payload):
    module = _discovery_module()

    assert module.is_discovery_query(payload) is False


def test_exact_ascii_v1_query_is_accepted():
    module = _discovery_module()

    assert module.is_discovery_query(b"alpacadiscovery1") is True


class _QueueSocket:
    def __init__(self, family, socket_type, *, ipv6_failure=False):
        self.family = family
        self.socket_type = socket_type
        self.ipv6_failure = ipv6_failure
        self.operations = []
        self.sent = []
        self._packets = []
        self._condition = threading.Condition()
        self._closed = False

    def setsockopt(self, level, option, value):
        self.operations.append(("setsockopt", level, option, value))

    def bind(self, address):
        self.operations.append(("bind", address))
        if self.family == socket.AF_INET6 and self.ipv6_failure:
            raise OSError("IPv6 unavailable")

    def settimeout(self, timeout):
        self.operations.append(("settimeout", timeout))

    def recvfrom(self, maximum):
        with self._condition:
            while not self._packets and not self._closed:
                self._condition.wait(timeout=0.05)
            if self._closed:
                raise OSError("closed")
            payload, source = self._packets.pop(0)
        return payload[:maximum], source

    def sendto(self, payload, source):
        self.sent.append((payload, source))

    def close(self):
        with self._condition:
            self._closed = True
            self.operations.append(("close",))
            self._condition.notify_all()

    def queue(self, payload, source=("127.0.0.1", 42000)):
        with self._condition:
            self._packets.append((payload, source))
            self._condition.notify_all()


class _SocketFactory:
    def __init__(self, *, ipv6_failure=True):
        self.ipv6_failure = ipv6_failure
        self.sockets = []

    def __call__(self, family, socket_type):
        sock = _QueueSocket(
            family,
            socket_type,
            ipv6_failure=self.ipv6_failure,
        )
        self.sockets.append(sock)
        return sock


class _BrokenReceiveSocket(_QueueSocket):
    def recvfrom(self, maximum):
        del maximum
        raise OSError("listener failed")


class _BrokenReceiveFactory(_SocketFactory):
    def __call__(self, family, socket_type):
        if family == socket.AF_INET:
            sock = _BrokenReceiveSocket(family, socket_type)
        else:
            sock = _QueueSocket(family, socket_type, ipv6_failure=True)
        self.sockets.append(sock)
        return sock


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        time.sleep(0.005)


def test_responder_sets_shared_options_before_bind_and_replies_to_source(caplog):
    module = _discovery_module()
    factory = _SocketFactory(ipv6_failure=True)
    responder = module.DiscoveryResponder(
        11111,
        discovery_port=32227,
        socket_factory=factory,
    )
    source = ("192.0.2.44", 42123)

    with caplog.at_level(logging.WARNING):
        responder.start()
        responder.start()
        ipv4 = next(sock for sock in factory.sockets if sock.family == socket.AF_INET)
        bind_index = next(
            index for index, operation in enumerate(ipv4.operations)
            if operation[0] == "bind"
        )
        reuse_index = next(
            index for index, operation in enumerate(ipv4.operations)
            if operation[:3] == ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR)
        )
        assert reuse_index < bind_index
        assert ipv4.operations[bind_index] == ("bind", ("", 32227))

        ipv4.queue(b"alpacadiscovery1", source)
        _wait_until(lambda: ipv4.sent)
        assert ipv4.sent == [(b'{"AlpacaPort":11111}', source)]

        for malicious in (
            b"",
            b"alpacadiscovery1extra",
            b"\xffalpacadiscovery1",
            b"x" * 2_048,
        ):
            ipv4.queue(malicious, ("198.51.100.8", 50000))
        time.sleep(0.05)
        assert ipv4.sent == [(b'{"AlpacaPort":11111}', source)]
        assert sum("IPv6" in record.message for record in caplog.records) == 1
    responder.stop()
    responder.stop()
    assert responder.running is False
    assert all(sock._closed for sock in factory.sockets)


def test_constructor_and_repeated_stop_have_no_socket_side_effects():
    module = _discovery_module()
    factory = _SocketFactory()

    responder = module.DiscoveryResponder(
        11111,
        discovery_port=32227,
        socket_factory=factory,
    )
    responder.stop()
    responder.stop()

    assert factory.sockets == []
    assert responder.running is False


def test_unexpected_listener_exit_closes_its_socket_without_waiting_for_stop():
    module = _discovery_module()
    factory = _BrokenReceiveFactory()
    responder = module.DiscoveryResponder(
        11111,
        discovery_port=32227,
        socket_factory=factory,
    )

    responder.start()
    ipv4 = next(sock for sock in factory.sockets if sock.family == socket.AF_INET)
    _wait_until(lambda: responder.running is False)

    assert ipv4._closed is True
    responder.stop()
