"""Thread-safe, bounded fan-out for coordinator state events."""

from __future__ import annotations

import queue
import threading
from typing import Generic, TypeVar, cast


EventT = TypeVar("EventT")
_CLOSED = object()


class EventSubscription(Generic[EventT]):
    """One independently bounded stream of broker events."""

    def __init__(
        self,
        broker: EventBroker[EventT],
        subscription_id: int,
        pending: queue.Queue[object],
    ) -> None:
        self._broker = broker
        self._subscription_id = subscription_id
        self._pending = pending
        self._closed = False
        self._close_lock = threading.Lock()

    def get(self, block: bool = True, timeout: float | None = None) -> EventT:
        with self._close_lock:
            closed = self._closed
        if closed and self._pending.empty():
            raise queue.Empty
        value = self._pending.get(block=block, timeout=timeout)
        if value is _CLOSED:
            raise queue.Empty
        return cast(EventT, value)

    def get_nowait(self) -> EventT:
        return self.get(block=False)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._broker._offer_latest(self._pending, _CLOSED)
        self._broker._unsubscribe(self._subscription_id)

    def _offer(self, event: EventT) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._broker._offer_latest(self._pending, event)

    def __enter__(self) -> EventSubscription[EventT]:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class EventBroker(Generic[EventT]):
    """Publish without waiting for slow subscribers.

    Each subscriber owns a fixed-size queue. When that queue is full, the
    oldest pending event is discarded so the subscriber converges on current
    state instead of accumulating unbounded history.
    """

    def __init__(self, max_pending: int = 32) -> None:
        if isinstance(max_pending, bool) or not isinstance(max_pending, int):
            raise ValueError("max_pending must be a positive integer")
        if max_pending <= 0:
            raise ValueError("max_pending must be a positive integer")
        self._max_pending = max_pending
        self._lock = threading.Lock()
        self._next_subscription_id = 0
        self._subscribers: dict[int, EventSubscription[EventT]] = {}

    def subscribe(self) -> EventSubscription[EventT]:
        pending: queue.Queue[object] = queue.Queue(maxsize=self._max_pending)
        with self._lock:
            subscription_id = self._next_subscription_id
            self._next_subscription_id += 1
            subscription = EventSubscription(self, subscription_id, pending)
            self._subscribers[subscription_id] = subscription
        return subscription

    def publish(self, event: EventT) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers.values())
        for subscription in subscribers:
            subscription._offer(event)

    @staticmethod
    def _offer_latest(pending: queue.Queue[object], event: object) -> None:
        while True:
            try:
                pending.put_nowait(event)
                return
            except queue.Full:
                try:
                    pending.get_nowait()
                except queue.Empty:
                    pass

    def _unsubscribe(self, subscription_id: int) -> None:
        with self._lock:
            self._subscribers.pop(subscription_id, None)
