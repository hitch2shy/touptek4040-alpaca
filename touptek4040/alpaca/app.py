"""Falcon application factory for Alpaca Camera routes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import falcon

from ..errors import (
    CameraBusyError,
    CameraError,
    CameraInvalidValueError,
    CameraNotConnectedError,
    CameraNotSupportedError,
    CameraOperationCancelledError,
    safe_exception_text,
)
from .camera_api import CameraApi
from .common_api import CommonApi
from .image_api import ImageApiResource
from .manifest import CAMERA_V4_ENDPOINTS, EndpointSpec
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
    method_response,
    property_response,
)

_DRIVER_ERROR = 0x500


def create_app(coordinator: Any, config: Any) -> falcon.App:
    """Create an application with every manifest-defined non-image route."""

    app = falcon.App()
    allocator = ServerTransactionIdAllocator()
    camera_api = CameraApi(coordinator, config)
    common_api = CommonApi(coordinator, camera_api)
    apis = {"common": common_api, "camera": camera_api}

    resources: dict[tuple[str, str], _EndpointResource] = {}
    for spec in CAMERA_V4_ENDPOINTS:
        if spec.category == "image":
            app.add_route(
                f"/api/v1/camera/0/{spec.command}",
                ImageApiResource(coordinator, spec.command, allocator),
            )
            continue
        api = apis[spec.category]
        key = (spec.category, spec.command)
        resource = resources.get(key)
        if resource is None:
            resource = _EndpointResource(api, allocator)
            resources[key] = resource
            app.add_route(
                f"/api/v1/camera/0/{spec.command}",
                resource,
            )
        resource.add(spec)
    app.add_sink(_invalid_path)
    return app


class _EndpointResource:
    def __init__(
        self,
        api: CommonApi | CameraApi,
        allocator: ServerTransactionIdAllocator,
    ) -> None:
        self._api = api
        self._allocator = allocator
        self._specs: dict[str, EndpointSpec] = {}

    def add(self, spec: EndpointSpec) -> None:
        self._specs[spec.http_method] = spec

    def on_get(self, request: falcon.Request, response: falcon.Response) -> None:
        self._respond("GET", request, response)

    def on_put(self, request: falcon.Request, response: falcon.Response) -> None:
        self._respond("PUT", request, response)

    def _respond(
        self,
        method: str,
        request: falcon.Request,
        response: falcon.Response,
    ) -> None:
        spec = self._specs.get(method)
        if spec is None:
            raise falcon.HTTPMethodNotAllowed(tuple(self._specs))
        server_transaction_id = self._allocator.allocate()
        try:
            client_id = _request_client_id(request)
            envelope_request = SimpleNamespace(client_transaction_id=client_id)
            if spec.http_method == "GET":
                value = self._api.get(spec.command, request)
                body = property_response(
                    value,
                    envelope_request,
                    server_transaction_id,
                )
            else:
                self._api.put(spec.command, request)
                body = method_response(envelope_request, server_transaction_id)
            response.status = falcon.HTTP_200
        except BaseException as error:
            translated, status = _translate_error(error)
            body = _error_response(
                request,
                server_transaction_id,
                translated,
            )
            response.status = status
        response.content_type = falcon.MEDIA_JSON
        response.media = body


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
        return AlpacaError(
            INVALID_OPERATION,
            safe_exception_text(error),
        ), falcon.HTTP_200
    if isinstance(error, CameraNotSupportedError):
        return AlpacaError(
            METHOD_NOT_IMPLEMENTED,
            safe_exception_text(error),
        ), falcon.HTTP_200
    if isinstance(error, CameraOperationCancelledError):
        return AlpacaError(
            OPERATION_CANCELLED,
            safe_exception_text(error),
        ), falcon.HTTP_200
    if isinstance(error, CameraError):
        return AlpacaError(
            _DRIVER_ERROR,
            safe_exception_text(error),
        ), falcon.HTTP_200
    return (
        AlpacaError(_DRIVER_ERROR, "An internal camera service error occurred"),
        falcon.HTTP_500,
    )


def _error_response(
    request: falcon.Request,
    server_transaction_id: int,
    error: AlpacaError,
) -> dict[str, int | str]:
    try:
        client_id = client_transaction_id(request)
    except AlpacaError:
        client_id = 0
    return {
        "ClientTransactionID": client_id,
        "ServerTransactionID": server_transaction_id,
        "ErrorNumber": error.number,
        "ErrorMessage": error.message,
    }


def _request_client_id(request: falcon.Request) -> int:
    if request.method == "PUT" and request.content_length in (None, 0):
        query_only = SimpleNamespace(method="GET", params=request.params)
        return client_transaction_id(query_only)
    return client_transaction_id(request)


def _invalid_path(
    request: falcon.Request,
    response: falcon.Response,
) -> None:
    del request
    response.status = falcon.HTTP_400
    response.content_type = falcon.MEDIA_JSON
    response.media = {
        "ErrorNumber": INVALID_VALUE,
        "ErrorMessage": "Invalid Alpaca API path",
    }
