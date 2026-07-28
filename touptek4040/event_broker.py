"""Thread-safe, bounded fan-out for coordinator state events."""

from __future__ import annotations

import queue
import threading
from typing import Generic, TypeVar


EventT = TypeVar("EventT")


class EventSubscription(Generic[EventT]):
    """One independently bounded stream of broker events."""

    def __init__(
        self,
        broker: EventBroker[EventT],
        subscription_id: int,
        pending: queue.Queue[EventT],
    ) -> None:
        self._broker = broker
        self._subscription_id = subscription_id
        self._pending = pending
        self._closed = False
        self._close_lock = threading.Lock()

    def get(self, block: bool = True, timeout: float | None = None) -> EventT:
        return self._pending.get(block=block, timeout=timeout)

    def get_nowait(self) -> EventT:
        return self._pending.get_nowait()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._broker._unsubscribe(self._subscription_id)

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
        self._subscribers: dict[int, queue.Queue[EventT]] = {}

    def subscribe(self) -> EventSubscription[EventT]:
        pending: queue.Queue[EventT] = queue.Queue(maxsize=self._max_pending)
        with self._lock:
            subscription_id = self._next_subscription_id
            self._next_subscription_id += 1
            self._subscribers[subscription_id] = pending
        return EventSubscription(self, subscription_id, pending)

    def publish(self, event: EventT) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers.values())
        for pending in subscribers:
            self._offer_latest(pending, event)

    @staticmethod
    def _offer_latest(pending: queue.Queue[EventT], event: EventT) -> None:
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
