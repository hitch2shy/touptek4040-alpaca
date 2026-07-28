from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from touptek4040.alpaca.request import client_transaction_id, get_parameter
from touptek4040.alpaca.responses import INVALID_VALUE, AlpacaError


@dataclass
class FakeRequest:
    method: str
    params: dict[str, Any] = field(default_factory=dict)
    media: Any = field(default_factory=dict)


@pytest.mark.parametrize(
    ("method", "params", "media", "expected"),
    [
        ("GET", {"CLIENTTRANSACTIONID": "17"}, {}, 17),
        ("GET", {"clientTransactionId": "18"}, {}, 18),
        ("PUT", {}, {"ClIeNtTrAnSaCtIoNiD": "19"}, 19),
    ],
)
def test_client_transaction_id_is_case_insensitive_for_get_and_put(
    method: str,
    params: dict[str, Any],
    media: dict[str, Any],
    expected: int,
) -> None:
    assert client_transaction_id(FakeRequest(method, params, media)) == expected


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_missing_client_transaction_id_defaults_to_zero(method: str) -> None:
    assert client_transaction_id(FakeRequest(method)) == 0


@pytest.mark.parametrize(
    "invalid",
    ["", "1.0", "-1", "+1", "not-an-integer", "4294967296", None, [], True],
)
def test_invalid_client_transaction_id_becomes_alpaca_invalid_value(
    invalid: Any,
) -> None:
    request = FakeRequest("GET", {"ClientTransactionID": invalid})

    with pytest.raises(AlpacaError) as exc_info:
        client_transaction_id(request)

    assert exc_info.value.number == INVALID_VALUE
    assert "ClientTransactionID" in exc_info.value.message


def test_get_parameters_are_case_insensitive() -> None:
    request = FakeRequest("GET", {"dUrAtIoN": "1.25"})

    assert get_parameter(request, "Duration") == "1.25"


def test_put_media_overrides_query_parameters_case_insensitively() -> None:
    request = FakeRequest(
        "PUT",
        {"Gain": "100", "ClientTransactionID": "7"},
        {"gAiN": "250", "CLIENTTRANSACTIONID": "8"},
    )

    assert get_parameter(request, "GAIN") == "250"
    assert client_transaction_id(request) == 8


def test_get_ignores_media_parameters() -> None:
    request = FakeRequest("GET", {"Gain": "100"}, {"Gain": "250"})

    assert get_parameter(request, "gain") == "100"


def test_optional_missing_parameter_returns_none() -> None:
    assert get_parameter(FakeRequest("PUT"), "Light", required=False) is None


def test_required_missing_parameter_becomes_alpaca_invalid_value() -> None:
    with pytest.raises(AlpacaError) as exc_info:
        get_parameter(FakeRequest("PUT"), "Duration")

    assert exc_info.value.number == INVALID_VALUE
    assert "Duration" in exc_info.value.message


class BrokenParamsRequest:
    method = "GET"

    @property
    def params(self) -> dict[str, str]:
        raise ValueError("framework query parser failed")


class BrokenMediaRequest:
    method = "PUT"
    params: dict[str, str] = {}

    @property
    def media(self) -> dict[str, str]:
        raise RuntimeError("framework media parser failed")


@pytest.mark.parametrize(
    "broken_request",
    [BrokenParamsRequest(), BrokenMediaRequest()],
)
def test_framework_parameter_parse_errors_do_not_leak(
    broken_request: object,
) -> None:
    with pytest.raises(AlpacaError) as exc_info:
        get_parameter(broken_request, "Duration")

    assert exc_info.value.number == INVALID_VALUE
    assert "request parameters" in exc_info.value.message.casefold()


def test_non_mapping_put_media_becomes_alpaca_invalid_value() -> None:
    with pytest.raises(AlpacaError) as exc_info:
        get_parameter(FakeRequest("PUT", media=["Duration", "1.0"]), "Duration")

    assert exc_info.value.number == INVALID_VALUE
    assert "request parameters" in exc_info.value.message.casefold()
