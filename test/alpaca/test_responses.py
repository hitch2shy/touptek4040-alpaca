from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from touptek4040.alpaca.responses import (
    ACTION_NOT_IMPLEMENTED,
    INVALID_OPERATION,
    INVALID_VALUE,
    METHOD_NOT_IMPLEMENTED,
    NOT_CONNECTED,
    OPERATION_CANCELLED,
    VALUE_NOT_SET,
    AlpacaError,
    ServerTransactionIdAllocator,
    method_response,
    property_response,
)


@dataclass(frozen=True)
class FakeRequest:
    client_transaction_id: int = 0


def test_success_property_response_has_exact_alpaca_fields_and_types() -> None:
    body = property_response(4, FakeRequest(client_transaction_id=17), 9)

    assert body == {
        "Value": 4,
        "ClientTransactionID": 17,
        "ServerTransactionID": 9,
        "ErrorNumber": 0,
        "ErrorMessage": "",
    }
    assert type(body["ClientTransactionID"]) is int
    assert type(body["ServerTransactionID"]) is int
    assert type(body["ErrorNumber"]) is int
    assert type(body["ErrorMessage"]) is str


def test_success_method_response_omits_value_and_has_exact_alpaca_fields() -> None:
    assert method_response(FakeRequest(client_transaction_id=23), 11) == {
        "ClientTransactionID": 23,
        "ServerTransactionID": 11,
        "ErrorNumber": 0,
        "ErrorMessage": "",
    }


ResponseFactory = Callable[[Any], dict[str, Any]]


@pytest.fixture(
    params=[
        pytest.param(
            lambda transaction_id: property_response(
                4,
                FakeRequest(client_transaction_id=17),
                transaction_id,
            ),
            id="property_response",
        ),
        pytest.param(
            lambda transaction_id: method_response(
                FakeRequest(client_transaction_id=17),
                transaction_id,
            ),
            id="method_response",
        ),
    ],
)
def response_factory(request: pytest.FixtureRequest) -> ResponseFactory:
    return request.param


@pytest.mark.parametrize("transaction_id", [1, 0xFFFFFFFF])
def test_response_helpers_accept_positive_uint32_server_transaction_ids(
    response_factory: ResponseFactory,
    transaction_id: int,
) -> None:
    assert response_factory(transaction_id)["ServerTransactionID"] == transaction_id


@pytest.mark.parametrize(
    "invalid_transaction_id",
    [True, 0, -1, 0x100000000, "1", 1.0, None],
)
def test_response_helpers_reject_invalid_server_transaction_ids_consistently(
    response_factory: ResponseFactory,
    invalid_transaction_id: Any,
) -> None:
    with pytest.raises(
        ValueError,
        match="transaction_id must be an integer from 1 through 4294967295",
    ):
        response_factory(invalid_transaction_id)


def test_official_alpaca_error_numbers_are_exact() -> None:
    assert {
        METHOD_NOT_IMPLEMENTED,
        INVALID_VALUE,
        VALUE_NOT_SET,
        NOT_CONNECTED,
        INVALID_OPERATION,
        ACTION_NOT_IMPLEMENTED,
        OPERATION_CANCELLED,
    } == {0x400, 0x401, 0x402, 0x407, 0x40B, 0x40C, 0x40E}


def test_alpaca_error_preserves_number_and_message() -> None:
    error = AlpacaError(INVALID_VALUE, "Gain must be between 100 and 1650")

    assert error.number == 0x401
    assert error.message == "Gain must be between 100 and 1650"
    assert str(error) == "Gain must be between 100 and 1650"


def test_server_transaction_allocator_starts_at_one_and_increments() -> None:
    allocator = ServerTransactionIdAllocator()

    assert [allocator.allocate() for _ in range(3)] == [1, 2, 3]


def test_server_transaction_allocator_is_thread_safe() -> None:
    allocator = ServerTransactionIdAllocator()

    with ThreadPoolExecutor(max_workers=16) as executor:
        allocated = list(executor.map(lambda _: allocator.allocate(), range(2_000)))

    assert len(set(allocated)) == 2_000
    assert set(allocated) == set(range(1, 2_001))


def test_server_transaction_allocator_wraps_from_uint32_max_to_one() -> None:
    allocator = ServerTransactionIdAllocator(next_id=0xFFFFFFFF)

    assert allocator.allocate() == 0xFFFFFFFF
    assert allocator.allocate() == 1
