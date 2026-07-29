"""Case-insensitive Alpaca request parameter parsing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .responses import INVALID_VALUE, AlpacaError, RequestParseError

_UINT32_MAX = 0xFFFFFFFF
_MISSING = object()


def _invalid_parameters(cause: Exception) -> RequestParseError:
    del cause
    return RequestParseError(INVALID_VALUE, "Invalid request parameters")


def _casefolded_fields(value: Any, source: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{source} parameters must be a mapping")

    fields: dict[str, Any] = {}
    for key, field_value in value.items():
        if not isinstance(key, str):
            raise TypeError(f"{source} parameter names must be strings")
        fields[key.casefold()] = field_value
    return fields


def _request_fields(request: Any) -> dict[str, Any]:
    try:
        fields = _casefolded_fields(request.params, "query")
        method = request.method
        if not isinstance(method, str):
            raise TypeError("request method must be a string")
        if method.casefold() == "put":
            fields.update(_casefolded_fields(request.media, "PUT media"))
        return fields
    except AlpacaError:
        raise
    except Exception as exc:
        raise _invalid_parameters(exc) from exc


def get_parameter(
    request: Any,
    name: str,
    required: bool = True,
) -> Any | None:
    """Return a case-insensitive parameter, with PUT media taking precedence."""

    try:
        value = _request_fields(request).get(name.casefold(), _MISSING)
    except AlpacaError:
        raise
    except Exception as exc:
        raise _invalid_parameters(exc) from exc

    if value is _MISSING:
        if required:
            raise RequestParseError(
                INVALID_VALUE,
                f"Missing required parameter: {name}",
            )
        return None
    return value


def client_transaction_id(request: Any) -> int:
    """Parse the optional uint32 ClientTransactionID, defaulting to zero."""

    value = _request_fields(request).get("clienttransactionid", _MISSING)
    if value is _MISSING:
        return 0

    if isinstance(value, bool):
        transaction_id = 0
    elif isinstance(value, int):
        transaction_id = value
    elif isinstance(value, str):
        if (
            len(value) > 10
            or not value.isascii()
            or not value.isdecimal()
        ):
            transaction_id = 0
        else:
            significant_digits = value.lstrip("0")
            if not significant_digits:
                transaction_id = 0
            else:
                try:
                    transaction_id = int(significant_digits)
                except (TypeError, ValueError, OverflowError):
                    transaction_id = 0
    else:
        transaction_id = 0

    if not 1 <= transaction_id <= _UINT32_MAX:
        raise RequestParseError(
            INVALID_VALUE,
            "ClientTransactionID must be an integer from 1 through 4294967295",
        )
    return transaction_id


def client_id(request: Any) -> int:
    """Validate the optional unsigned 32-bit Alpaca ClientID."""

    value = _request_fields(request).get("clientid", _MISSING)
    if value is _MISSING:
        return 0

    if isinstance(value, bool):
        parsed = -1
    elif isinstance(value, int):
        parsed = value
    elif (
        isinstance(value, str)
        and len(value) <= 10
        and value.isascii()
        and value.isdecimal()
    ):
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            parsed = -1
    else:
        parsed = -1

    if not 0 <= parsed <= _UINT32_MAX:
        raise RequestParseError(
            INVALID_VALUE,
            "ClientID must be an unsigned 32-bit integer",
        )
    return parsed
