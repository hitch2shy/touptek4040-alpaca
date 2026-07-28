"""Standard Alpaca response envelopes, errors, and transaction allocation."""

from __future__ import annotations

from threading import Lock
from typing import Any

METHOD_NOT_IMPLEMENTED = 0x400
INVALID_VALUE = 0x401
VALUE_NOT_SET = 0x402
NOT_CONNECTED = 0x407
INVALID_OPERATION = 0x40B
ACTION_NOT_IMPLEMENTED = 0x40C
OPERATION_CANCELLED = 0x40E

_UINT32_MAX = 0xFFFFFFFF


class AlpacaError(Exception):
    """A device-level error that must be returned in an Alpaca envelope."""

    def __init__(self, number: int, message: str) -> None:
        self.number = number
        self.message = message
        super().__init__(message)


class ServerTransactionIdAllocator:
    """Allocate non-zero uint32 server transaction IDs safely across threads."""

    def __init__(self, *, next_id: int = 1) -> None:
        if (
            isinstance(next_id, bool)
            or not isinstance(next_id, int)
            or not 1 <= next_id <= _UINT32_MAX
        ):
            raise ValueError("next_id must be an integer from 1 through 4294967295")
        self._next_id = next_id
        self._lock = Lock()

    def allocate(self) -> int:
        with self._lock:
            allocated = self._next_id
            self._next_id = 1 if allocated == _UINT32_MAX else allocated + 1
            return allocated


def _client_transaction_id(request: Any) -> int:
    try:
        transaction_id = request.client_transaction_id
    except AttributeError:
        from .request import client_transaction_id

        return client_transaction_id(request)

    if (
        isinstance(transaction_id, bool)
        or not isinstance(transaction_id, int)
        or not 0 <= transaction_id <= _UINT32_MAX
    ):
        raise AlpacaError(
            INVALID_VALUE,
            "ClientTransactionID must be an unsigned 32-bit integer",
        )
    return transaction_id


def _response_fields(
    request: Any,
    transaction_id: int,
) -> dict[str, int | str]:
    if (
        isinstance(transaction_id, bool)
        or not isinstance(transaction_id, int)
        or not 1 <= transaction_id <= _UINT32_MAX
    ):
        raise ValueError(
            "transaction_id must be an integer from 1 through 4294967295"
        )
    return {
        "ClientTransactionID": _client_transaction_id(request),
        "ServerTransactionID": transaction_id,
        "ErrorNumber": 0,
        "ErrorMessage": "",
    }


def property_response(
    value: Any,
    request: Any,
    transaction_id: int,
) -> dict[str, Any]:
    """Return a successful Alpaca property response."""

    return {
        "Value": value,
        **_response_fields(request, transaction_id),
    }


def method_response(request: Any, transaction_id: int) -> dict[str, int | str]:
    """Return a successful Alpaca method response."""

    return _response_fields(request, transaction_id)
