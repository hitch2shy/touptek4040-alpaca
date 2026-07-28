import queue
import time

import pytest

from touptek4040.event_broker import EventBroker


def test_each_subscription_receives_published_events_in_order():
    broker = EventBroker(max_pending=3)
    first = broker.subscribe()
    second = broker.subscribe()

    broker.publish("connected")
    broker.publish("exposing")

    assert first.get_nowait() == "connected"
    assert first.get_nowait() == "exposing"
    assert second.get_nowait() == "connected"
    assert second.get_nowait() == "exposing"


def test_slow_subscription_is_bounded_and_retains_newest_events():
    broker = EventBroker(max_pending=2)
    subscription = broker.subscribe()

    started = time.monotonic()
    for event in range(10_000):
        broker.publish(event)

    assert time.monotonic() - started < 0.5
    assert subscription.get_nowait() == 9_998
    assert subscription.get_nowait() == 9_999
    with pytest.raises(queue.Empty):
        subscription.get_nowait()


def test_closed_subscription_receives_no_more_events():
    broker = EventBroker(max_pending=1)
    subscription = broker.subscribe()
    subscription.close()

    broker.publish("ignored")

    with pytest.raises(queue.Empty):
        subscription.get_nowait()


def test_pending_limit_must_be_positive():
    with pytest.raises(ValueError):
        EventBroker(max_pending=0)
