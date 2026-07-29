from __future__ import annotations

import queue
from collections import deque
from types import SimpleNamespace

from touptek4040.web_console import WebConsole, _SseStream


class TrackingSubscription:
    def __init__(self, broker) -> None:
        self._broker = broker
        self._closed = False

    def get(self, timeout: float):
        del timeout
        if self._broker.pending:
            return self._broker.pending.popleft()
        raise queue.Empty

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._broker.active -= 1


class TrackingBroker:
    def __init__(self) -> None:
        self.active = 0
        self.pending = deque()

    def subscribe(self) -> TrackingSubscription:
        self.active += 1
        return TrackingSubscription(self)


def test_sse_emits_generation_state_heartbeat_and_releases_disconnect(
    web_coordinator,
    web_config,
):
    broker = TrackingBroker()
    web_coordinator.events = broker
    console = WebConsole(web_coordinator, web_config)
    stream = _SseStream(console, heartbeat_seconds=0.001)
    iterator = iter(stream)

    initial = next(iterator)
    heartbeat = next(iterator)
    broker.pending.append(SimpleNamespace())
    changed = next(iterator)

    assert broker.active == 1
    assert initial.startswith(b"event: state\n")
    assert b'"generation":1' in initial
    assert b'"cameraState":"idle"' in initial
    assert heartbeat == b": heartbeat\n\n"
    assert changed.startswith(b"event: state\n")

    iterator.close()
    assert broker.active == 0


def test_sse_close_is_safe_before_iteration(web_coordinator, web_config):
    broker = TrackingBroker()
    web_coordinator.events = broker
    stream = _SseStream(
        WebConsole(web_coordinator, web_config),
        heartbeat_seconds=0.001,
    )

    stream.close()
    iterator = iter(stream)
    initial = next(iterator)

    assert b'"generation":1' in initial
    try:
        next(iterator)
    except StopIteration:
        pass
    else:
        raise AssertionError("closed SSE stream must stop after its initial state")
    assert broker.active == 0
