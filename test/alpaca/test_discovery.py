from __future__ import annotations

import importlib
import json
import logging
import socket
import struct
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
    def __init__(
        self,
        family,
        socket_type,
        *,
        ipv6_failure=False,
        join_fail_indexes=(),
    ):
        self.family = family
        self.socket_type = socket_type
        self.ipv6_failure = ipv6_failure
        self.join_fail_indexes = set(join_fail_indexes)
        self.operations = []
        self.sent = []
        self._packets = []
        self._condition = threading.Condition()
        self._closed = False

    def setsockopt(self, level, option, value):
        self.operations.append(("setsockopt", level, option, value))
        if (
            option == socket.IPV6_JOIN_GROUP
            and struct.unpack("@I", value[-4:])[0] in self.join_fail_indexes
        ):
            raise OSError("interface cannot join multicast")

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
    def __init__(self, *, ipv6_failure=True, join_fail_indexes=()):
        self.ipv6_failure = ipv6_failure
        self.join_fail_indexes = set(join_fail_indexes)
        self.sockets = []

    def __call__(self, family, socket_type):
        sock = _QueueSocket(
            family,
            socket_type,
            ipv6_failure=self.ipv6_failure,
            join_fail_indexes=self.join_fail_indexes,
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


class _RecoveringReceiveFactory(_SocketFactory):
    def __init__(self):
        super().__init__(ipv6_failure=False)
        self._ipv4_count = 0

    def __call__(self, family, socket_type):
        if family == socket.AF_INET:
            self._ipv4_count += 1
            socket_class = (
                _BrokenReceiveSocket if self._ipv4_count == 1 else _QueueSocket
            )
            sock = socket_class(family, socket_type)
        else:
            sock = _QueueSocket(family, socket_type)
        self.sockets.append(sock)
        return sock


class _BlockingCloseSocket(_QueueSocket):
    def __init__(self, family, socket_type):
        super().__init__(family, socket_type)
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        self._blocked_once = False

    def close(self):
        if not self._blocked_once:
            self._blocked_once = True
            self.close_entered.set()
            self.close_release.wait(timeout=1.0)
        super().close()


class _StopStartRaceFactory(_SocketFactory):
    def __init__(self):
        super().__init__(ipv6_failure=True)
        self._ipv4_count = 0

    def __call__(self, family, socket_type):
        if family == socket.AF_INET:
            self._ipv4_count += 1
            sock = (
                _BlockingCloseSocket(family, socket_type)
                if self._ipv4_count == 1
                else _QueueSocket(family, socket_type)
            )
        else:
            sock = _QueueSocket(
                family,
                socket_type,
                ipv6_failure=True,
            )
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


def test_ipv4_failure_atomically_stops_ipv6_and_restart_restores_response():
    module = _discovery_module()
    factory = _RecoveringReceiveFactory()
    responder = module.DiscoveryResponder(
        11111,
        discovery_port=32227,
        socket_factory=factory,
        interface_enumerator=lambda: [(7, "Ethernet")],
    )

    responder.start()
    first_ipv6 = next(
        sock for sock in factory.sockets if sock.family == socket.AF_INET6
    )
    _wait_until(lambda: responder.running is False)
    _wait_until(lambda: first_ipv6._closed)

    responder.start()
    second_ipv4 = [
        sock for sock in factory.sockets if sock.family == socket.AF_INET
    ][1]
    _wait_until(lambda: responder.running)
    second_ipv4.queue(b"alpacadiscovery1", ("127.0.0.1", 43000))
    _wait_until(lambda: second_ipv4.sent)

    assert second_ipv4.sent == [
        (b'{"AlpacaPort":11111}', ("127.0.0.1", 43000))
    ]
    assert len(
        [sock for sock in factory.sockets if sock.family == socket.AF_INET]
    ) == 2
    responder.stop()
    assert all(sock._closed for sock in factory.sockets)


def test_ipv6_joins_every_interface_and_replies_to_scoped_source():
    module = _discovery_module()
    factory = _SocketFactory(ipv6_failure=False)
    responder = module.DiscoveryResponder(
        11111,
        socket_factory=factory,
        interface_enumerator=lambda: [(3, "Ethernet"), (7, "Wi-Fi")],
    )
    source = ("fe80::1234", 44000, 0, 7)

    responder.start()
    ipv6 = next(sock for sock in factory.sockets if sock.family == socket.AF_INET6)
    memberships = [
        operation[3]
        for operation in ipv6.operations
        if operation[:3]
        == ("setsockopt", socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP)
    ]
    assert [struct.unpack("@I", value[-4:])[0] for value in memberships] == [3, 7]
    assert all(
        value[:16] == socket.inet_pton(socket.AF_INET6, "ff12::a1:9aca")
        for value in memberships
    )

    ipv6.queue(b"alpacadiscovery1", source)
    _wait_until(lambda: ipv6.sent)
    assert ipv6.sent == [(b'{"AlpacaPort":11111}', source)]
    responder.stop()


def test_ipv6_partial_join_failure_keeps_successful_interface(caplog):
    module = _discovery_module()
    factory = _SocketFactory(
        ipv6_failure=False,
        join_fail_indexes={3},
    )
    responder = module.DiscoveryResponder(
        11111,
        socket_factory=factory,
        interface_enumerator=lambda: [(3, "Ethernet"), (7, "Wi-Fi")],
    )

    with caplog.at_level(logging.WARNING):
        responder.start()
        ipv6 = next(
            sock for sock in factory.sockets if sock.family == socket.AF_INET6
        )
        ipv6.queue(
            b"alpacadiscovery1",
            ("fe80::5678", 45000, 0, 7),
        )
        _wait_until(lambda: ipv6.sent)

    assert any(
        "interface 3" in record.message
        for record in caplog.records
    )
    assert responder.running is True
    responder.stop()


def test_ipv6_all_join_failures_degrade_to_ipv4_and_warn_only_once(caplog):
    module = _discovery_module()
    factory = _SocketFactory(
        ipv6_failure=False,
        join_fail_indexes={3, 7},
    )
    responder = module.DiscoveryResponder(
        11111,
        socket_factory=factory,
        interface_enumerator=lambda: [(3, "Ethernet"), (7, "Wi-Fi")],
    )

    with caplog.at_level(logging.WARNING):
        responder.start()
        assert responder.running is True
        responder.stop()
        responder.start()
        assert responder.running is True
        responder.stop()

    assert sum(
        "IPv6 Alpaca discovery is unavailable" in record.message
        for record in caplog.records
    ) == 1
    assert all(
        sock._closed
        for sock in factory.sockets
        if sock.family == socket.AF_INET6
    )


def test_overlapping_stop_and_start_retire_old_generation_without_leaks():
    module = _discovery_module()
    factory = _StopStartRaceFactory()
    responder = module.DiscoveryResponder(
        11111,
        socket_factory=factory,
    )
    responder.start()
    first_ipv4 = next(
        sock for sock in factory.sockets if sock.family == socket.AF_INET
    )

    stopping = threading.Thread(target=responder.stop)
    starting = threading.Thread(target=responder.start)
    stopping.start()
    assert first_ipv4.close_entered.wait(timeout=1.0)
    starting.start()
    first_ipv4.close_release.set()
    stopping.join(timeout=2.0)
    starting.join(timeout=2.0)

    assert stopping.is_alive() is False
    assert starting.is_alive() is False
    assert responder.running is True
    second_ipv4 = [
        sock for sock in factory.sockets if sock.family == socket.AF_INET
    ][1]
    second_ipv4.queue(b"alpacadiscovery1", ("127.0.0.1", 46000))
    _wait_until(lambda: second_ipv4.sent)
    assert first_ipv4._closed is True

    responder.stop()
    assert all(sock._closed for sock in factory.sockets)
