"""Case-insensitive Alpaca request parameter parsing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .responses import INVALID_VALUE, AlpacaError

_UINT32_MAX = 0xFFFFFFFF
_MISSING = object()


def _invalid_parameters(cause: Exception) -> AlpacaError:
    return AlpacaError(INVALID_VALUE, f"Invalid request parameters: {cause}")


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
            raise AlpacaError(INVALID_VALUE, f"Missing required parameter: {name}")
        return None
    return value


def client_transaction_id(request: Any) -> int:
    """Parse the optional uint32 ClientTransactionID, defaulting to zero."""

    value = _request_fields(request).get("clienttransactionid", _MISSING)
    if value is _MISSING:
        return 0

    if isinstance(value, bool):
        valid = False
    elif isinstance(value, int):
        valid = 0 <= value <= _UINT32_MAX
    elif isinstance(value, str):
        valid = value.isascii() and value.isdecimal()
    else:
        valid = False

    if not valid:
        raise AlpacaError(
            INVALID_VALUE,
            "ClientTransactionID must be an unsigned 32-bit integer",
        )

    transaction_id = int(value)
    if transaction_id > _UINT32_MAX:
        raise AlpacaError(
            INVALID_VALUE,
            "ClientTransactionID must be an unsigned 32-bit integer",
        )
    return transaction_id
