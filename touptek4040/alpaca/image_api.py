"""Bounded Alpaca JSON and ImageBytes image streaming."""

from __future__ import annotations

import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import falcon
import numpy as np

from ..errors import (
    CameraBusyError,
    CameraError,
    CameraInvalidValueError,
    CameraNotConnectedError,
    CameraNotSupportedError,
    CameraOperationCancelledError,
    safe_exception_text,
)
from ..image_workspace import ImageGeneration
from .request import client_transaction_id
from .responses import (
    INVALID_OPERATION,
    INVALID_VALUE,
    METHOD_NOT_IMPLEMENTED,
    NOT_CONNECTED,
    OPERATION_CANCELLED,
    AlpacaError,
    RequestParseError,
    ServerTransactionIdAllocator,
)

IMAGEBYTES_MEDIA_TYPE = "application/imagebytes"
_METADATA_V1 = struct.Struct("<iiIIiiiiiii")
_METADATA_SIZE = 44
_BYTE_ELEMENT_TYPE = 6
_UINT16_ELEMENT_TYPE = 8
_DRIVER_ERROR = 0x500
_UINT32_MAX = 0xFFFFFFFF
_PIXEL_ASCII = tuple(str(value).encode("ascii") for value in range(4096))


@dataclass(frozen=True, slots=True)
class ImageTransactions:
    client_transaction_id: int
    server_transaction_id: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.client_transaction_id, bool)
            or not isinstance(self.client_transaction_id, int)
            or not 0 <= self.client_transaction_id <= _UINT32_MAX
        ):
            raise ValueError("client_transaction_id must be an unsigned uint32")
        if (
            isinstance(self.server_transaction_id, bool)
            or not isinstance(self.server_transaction_id, int)
            or not 1 <= self.server_transaction_id <= _UINT32_MAX
        ):
            raise ValueError("server_transaction_id must be a positive uint32")


def iter_x_first_columns(
    pixels: np.ndarray,
    columns_per_chunk: int = 16,
) -> Iterator[np.ndarray]:
    """Yield bounded Y-by-X views whose columns map to ASCOM's first axis."""

    if (
        isinstance(columns_per_chunk, bool)
        or not isinstance(columns_per_chunk, int)
        or columns_per_chunk < 1
    ):
        raise ValueError("columns_per_chunk must be a positive integer")
    for x0 in range(0, pixels.shape[1], columns_per_chunk):
        yield pixels[:, x0 : x0 + columns_per_chunk]


def stream_image_json(
    generation: ImageGeneration,
    transactions: ImageTransactions,
) -> Iterable[bytes]:
    """Stream a complete Alpaca property envelope with Value[x][y]."""

    yield b'{"Value":['
    first_column = True
    for block in iter_x_first_columns(generation.pixels):
        encoded_columns: list[bytes] = []
        for relative_x in range(block.shape[1]):
            column = block[:, relative_x]
            encoded_values = b",".join(
                _PIXEL_ASCII[int(value)] for value in column
            )
            encoded_columns.append(b"[" + encoded_values + b"]")
        if encoded_columns:
            if not first_column:
                yield b","
            yield b",".join(encoded_columns)
            first_column = False
    yield (
        b'],"ClientTransactionID":'
        + str(transactions.client_transaction_id).encode("ascii")
        + b',"ServerTransactionID":'
        + str(transactions.server_transaction_id).encode("ascii")
        + b',"ErrorNumber":0,"ErrorMessage":""}'
    )


def stream_imagebytes(
    generation: ImageGeneration,
    transactions: ImageTransactions,
) -> Iterable[bytes]:
    """Stream official ImageBytes metadata v1 followed by x-first pixels."""

    element_type = (
        _UINT16_ELEMENT_TYPE
        if generation.pixels.dtype == np.dtype(np.uint16)
        else _BYTE_ELEMENT_TYPE
    )
    yield _metadata(
        error_number=0,
        transactions=transactions,
        image_element_type=element_type,
        transmission_element_type=element_type,
        rank=2,
        dimension1=generation.width,
        dimension2=generation.height,
        dimension3=0,
    )
    transmission_dtype = (
        np.dtype("<u2")
        if element_type == _UINT16_ELEMENT_TYPE
        else np.dtype("u1")
    )
    for block in iter_x_first_columns(generation.pixels):
        x_first = np.ascontiguousarray(block.T, dtype=transmission_dtype)
        yield x_first.tobytes(order="C")


def stream_imagebytes_error(
    error: AlpacaError,
    transactions: ImageTransactions,
) -> Iterable[bytes]:
    yield _metadata(
        error_number=error.number,
        transactions=transactions,
        image_element_type=0,
        transmission_element_type=0,
        rank=0,
        dimension1=0,
        dimension2=0,
        dimension3=0,
    )
    yield error.message.encode("utf-8")


class ImageApiResource:
    """Falcon resource for ImageArray and ImageArrayVariant."""

    def __init__(
        self,
        coordinator: Any,
        command: str,
        allocator: ServerTransactionIdAllocator,
    ) -> None:
        self._coordinator = coordinator
        self._command = command
        self._allocator = allocator

    def on_get(
        self,
        request: falcon.Request,
        response: falcon.Response,
    ) -> None:
        wants_imagebytes = _accepts_imagebytes(request)
        server_id = self._allocator.allocate()
        client_id = 0
        try:
            client_id = client_transaction_id(request)
            transactions = ImageTransactions(client_id, server_id)
            if self._command == "imagearrayvariant":
                raise AlpacaError(
                    METHOD_NOT_IMPLEMENTED,
                    "imagearrayvariant is not implemented by this camera",
                )
            generation = self._formal_generation()
            response.status = falcon.HTTP_200
            if wants_imagebytes:
                response.content_type = IMAGEBYTES_MEDIA_TYPE
                response.stream = stream_imagebytes(generation, transactions)
            else:
                response.content_type = falcon.MEDIA_JSON
                response.stream = stream_image_json(generation, transactions)
            return
        except BaseException as caught:
            error, status = _translate_error(caught)

        transactions = ImageTransactions(client_id, server_id)
        response.status = status
        if wants_imagebytes:
            response.content_type = IMAGEBYTES_MEDIA_TYPE
            response.stream = stream_imagebytes_error(error, transactions)
        else:
            response.content_type = falcon.MEDIA_JSON
            response.media = {
                "ClientTransactionID": client_id,
                "ServerTransactionID": server_id,
                "ErrorNumber": error.number,
                "ErrorMessage": error.message,
            }

    def _formal_generation(self) -> ImageGeneration:
        result = self._coordinator.formal_image_read()
        if not result.connected:
            raise AlpacaError(NOT_CONNECTED, "Camera is not connected")
        if result.error is not None:
            raise AlpacaError(
                _DRIVER_ERROR,
                safe_exception_text(result.error),
            )
        if not result.image_ready or result.generation is None:
            raise AlpacaError(
                INVALID_OPERATION,
                "No completed exposure image is available",
            )
        return result.generation


def _metadata(
    *,
    error_number: int,
    transactions: ImageTransactions,
    image_element_type: int,
    transmission_element_type: int,
    rank: int,
    dimension1: int,
    dimension2: int,
    dimension3: int,
) -> bytes:
    return _METADATA_V1.pack(
        1,
        error_number,
        transactions.client_transaction_id,
        transactions.server_transaction_id,
        _METADATA_SIZE,
        image_element_type,
        transmission_element_type,
        rank,
        dimension1,
        dimension2,
        dimension3,
    )


def _accepts_imagebytes(request: falcon.Request) -> bool:
    accept = request.get_header("Accept")
    if accept is None:
        return False
    for item in accept.split(","):
        parts = [part.strip() for part in item.split(";")]
        if not parts or parts[0].casefold() != IMAGEBYTES_MEDIA_TYPE:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            name, separator, value = parameter.partition("=")
            if separator and name.strip().casefold() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        if quality > 0:
            return True
    return False


def _translate_error(error: BaseException) -> tuple[AlpacaError, str]:
    if isinstance(error, RequestParseError):
        return error, falcon.HTTP_400
    if isinstance(error, AlpacaError):
        return error, falcon.HTTP_200
    if isinstance(error, CameraInvalidValueError):
        return AlpacaError(INVALID_VALUE, safe_exception_text(error)), falcon.HTTP_200
    if isinstance(error, CameraNotConnectedError):
        return AlpacaError(NOT_CONNECTED, safe_exception_text(error)), falcon.HTTP_200
    if isinstance(error, CameraBusyError):
        return (
            AlpacaError(INVALID_OPERATION, safe_exception_text(error)),
            falcon.HTTP_200,
        )
    if isinstance(error, CameraNotSupportedError):
        return (
            AlpacaError(METHOD_NOT_IMPLEMENTED, safe_exception_text(error)),
            falcon.HTTP_200,
        )
    if isinstance(error, CameraOperationCancelledError):
        return (
            AlpacaError(OPERATION_CANCELLED, safe_exception_text(error)),
            falcon.HTTP_200,
        )
    if isinstance(error, CameraError):
        return AlpacaError(_DRIVER_ERROR, safe_exception_text(error)), falcon.HTTP_200
    return (
        AlpacaError(_DRIVER_ERROR, "An internal camera service error occurred"),
        falcon.HTTP_500,
    )
