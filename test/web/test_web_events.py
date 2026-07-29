from __future__ import annotations

import queue
import threading
from collections import deque
from types import SimpleNamespace

import pytest

from touptek4040.event_broker import EventBroker
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


def test_sse_close_before_iteration_never_subscribes_or_emits(
    web_coordinator,
    web_config,
):
    broker = TrackingBroker()
    web_coordinator.events = broker
    stream = _SseStream(
        WebConsole(web_coordinator, web_config),
        heartbeat_seconds=0.001,
    )

    stream.close()
    iterator = iter(stream)

    with pytest.raises(StopIteration):
        next(iterator)
    assert broker.active == 0


def test_sse_close_after_initial_frame_stops_before_next_event(
    web_coordinator,
    web_config,
):
    broker = TrackingBroker()
    web_coordinator.events = broker
    stream = _SseStream(WebConsole(web_coordinator, web_config), 30)
    iterator = iter(stream)

    assert next(iterator).startswith(b"event: state\n")
    stream.close()

    with pytest.raises(StopIteration):
        next(iterator)
    assert broker.active == 0


def test_sse_close_immediately_unblocks_a_waiting_next(
    web_coordinator,
    web_config,
):
    broker = EventBroker(max_pending=2)
    web_coordinator.events = broker
    stream = _SseStream(WebConsole(web_coordinator, web_config), 30)
    iterator = iter(stream)
    next(iterator)
    stopped = threading.Event()

    def wait_for_next():
        try:
            next(iterator)
        except StopIteration:
            stopped.set()

    waiting = threading.Thread(target=wait_for_next, daemon=True)
    waiting.start()
    stream.close()
    waiting.join(0.5)

    assert stopped.is_set()


def test_abandoned_first_frame_can_be_closed_without_a_subscription(
    web_coordinator,
    web_config,
):
    broker = TrackingBroker()
    web_coordinator.events = broker
    stream = _SseStream(WebConsole(web_coordinator, web_config), 30)
    iterator = iter(stream)

    iterator.close()

    assert broker.active == 0
