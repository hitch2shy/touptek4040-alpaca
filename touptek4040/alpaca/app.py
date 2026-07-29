"""Falcon application factory for Alpaca Camera routes."""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import Any

import falcon

from ..web_console import SecurityHeadersMiddleware, register_web_console
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
from .management import (
    DeviceSetupResource,
    MainSetupResource,
    ManagementResource,
    invalid_setup_path,
    management_values,
)
from .manifest import CAMERA_V4_ENDPOINTS, EndpointSpec
from .request import client_id, client_transaction_id
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
_LOG = logging.getLogger(__name__)


def create_app(coordinator: Any, config: Any) -> falcon.App:
    """Create an application with every manifest-defined non-image route."""

    app = falcon.App(
        middleware=[_RequestLoggingMiddleware(), SecurityHeadersMiddleware()]
    )
    allocator = ServerTransactionIdAllocator()
    camera_api = CameraApi(coordinator, config)
    common_api = CommonApi(coordinator, camera_api)
    apis = {"common": common_api, "camera": camera_api}
    description, configured_devices = management_values(config)

    app.add_route(
        "/management/apiversions",
        ManagementResource([1], allocator),
    )
    app.add_route(
        "/management/v1/description",
        ManagementResource(description, allocator),
    )
    app.add_route(
        "/management/v1/configureddevices",
        ManagementResource(configured_devices, allocator),
    )
    app.add_route("/setup", MainSetupResource(config))
    app.add_route(
        "/setup/v1/{device_type}/{device_number}/setup",
        DeviceSetupResource(),
    )
    web_config = getattr(config, "web", None)
    if web_config is None or getattr(web_config, "enabled", True):
        register_web_console(app, coordinator, config)

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


class _RequestLoggingMiddleware:
    """Record route and outcome without logging query strings or request bodies."""

    def process_request(self, request: falcon.Request, response: falcon.Response) -> None:
        del response
        request.context.request_started = time.monotonic()

    def process_response(
        self,
        request: falcon.Request,
        response: falcon.Response,
        resource: object,
        req_succeeded: bool,
    ) -> None:
        del resource
        started = getattr(request.context, "request_started", time.monotonic())
        _LOG.info(
            "%s %s -> %s in %.1f ms%s",
            request.method,
            request.path,
            response.status_code,
            (time.monotonic() - started) * 1000.0,
            "" if req_succeeded else " (request failed)",
        )


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
    client_id(request)
    return client_transaction_id(request)


def _invalid_path(
    request: falcon.Request,
    response: falcon.Response,
) -> None:
    if request.path.startswith("/setup"):
        invalid_setup_path(response)
        return
    response.status = falcon.HTTP_400
    response.content_type = falcon.MEDIA_JSON
    response.media = {
        "ErrorNumber": INVALID_VALUE,
        "ErrorMessage": "Invalid Alpaca API path",
    }
