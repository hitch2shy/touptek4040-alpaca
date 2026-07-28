"""ASCOM Alpaca protocol primitives for the ToupTek camera service."""

from .manifest import CAMERA_V4_ENDPOINTS, EndpointSpec
from .request import client_transaction_id, get_parameter
from .responses import (
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

__all__ = [
    "ACTION_NOT_IMPLEMENTED",
    "AlpacaError",
    "CAMERA_V4_ENDPOINTS",
    "EndpointSpec",
    "INVALID_OPERATION",
    "INVALID_VALUE",
    "METHOD_NOT_IMPLEMENTED",
    "NOT_CONNECTED",
    "OPERATION_CANCELLED",
    "ServerTransactionIdAllocator",
    "VALUE_NOT_SET",
    "client_transaction_id",
    "get_parameter",
    "method_response",
    "property_response",
]
