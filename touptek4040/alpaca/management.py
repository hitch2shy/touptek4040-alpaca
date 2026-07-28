"""Official Alpaca management JSON and read-only setup resources."""

from __future__ import annotations

from html import escape
from types import SimpleNamespace
from typing import Any, Callable
import uuid

import falcon

from .common_api import (
    DEVICE_DESCRIPTION,
    DEVICE_NAME,
    DRIVER_VERSION,
)
from .request import client_transaction_id
from .responses import (
    AlpacaError,
    ServerTransactionIdAllocator,
    property_response,
)

SERVER_NAME = "ToupTek 4040 Alpaca Server"
MANUFACTURER = "Hangzhou ToupTek Photonics Co., Ltd."
LOCATION = "Local"
DEVICE_TYPE = "Camera"
DEVICE_NUMBER = 0
SIMULATOR_SERIAL = "SIM-4040"

_UID_PREFIX = "touptek4040:camera:"


def stable_unique_id(serial: str | None, source: str | None = None) -> str:
    """Derive an ASCII UUID from stable camera identity, never process hash state."""

    identity = serial.strip() if isinstance(serial, str) else ""
    if not identity and isinstance(source, str):
        if source.strip().casefold() in {"sim", "simulator"}:
            identity = SIMULATOR_SERIAL
    if not identity:
        raise ValueError(
            "real camera serial must be resolved before creating the Alpaca app"
        )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{_UID_PREFIX}{identity}"))


class ManagementResource:
    """Return one exact successful Alpaca property envelope."""

    def __init__(
        self,
        value: object | Callable[[], object],
        allocator: ServerTransactionIdAllocator,
    ) -> None:
        self._value = value
        self._allocator = allocator

    def on_get(self, request: falcon.Request, response: falcon.Response) -> None:
        server_transaction_id = self._allocator.allocate()
        try:
            client_id = client_transaction_id(request)
            envelope_request = SimpleNamespace(client_transaction_id=client_id)
            value = self._value() if callable(self._value) else self._value
            response.media = property_response(
                value,
                envelope_request,
                server_transaction_id,
            )
            response.status = falcon.HTTP_200
        except AlpacaError as error:
            response.media = {
                "ClientTransactionID": 0,
                "ServerTransactionID": server_transaction_id,
                "ErrorNumber": error.number,
                "ErrorMessage": error.message,
            }
            response.status = falcon.HTTP_400
        response.content_type = falcon.MEDIA_JSON


class MainSetupResource:
    """Safe information-only main setup page."""

    def on_get(self, request: falcon.Request, response: falcon.Response) -> None:
        del request
        response.status = falcon.HTTP_200
        response.content_type = falcon.MEDIA_HTML
        response.text = _html_page(
            SERVER_NAME,
            (
                f"<h1>{escape(SERVER_NAME)}</h1>"
                f"<p>Manufacturer: {escape(MANUFACTURER)}</p>"
                f"<p>Version: {escape(DRIVER_VERSION)}</p>"
                f"<p>Location: {escape(LOCATION)}</p>"
                "<p>This is a read-only information page. "
                "No hardware or service settings are changed here.</p>"
                '<p><a href="/setup/v1/camera/0/setup">'
                "Camera 0 information</a></p>"
            ),
        )


class DeviceSetupResource:
    """Device setup URL required by Alpaca, explicitly information-only."""

    def on_get(
        self,
        request: falcon.Request,
        response: falcon.Response,
        device_type: str,
        device_number: str,
    ) -> None:
        del request
        if device_type != "camera" or device_number != "0":
            _unsupported_setup(response)
            return
        response.status = falcon.HTTP_200
        response.content_type = falcon.MEDIA_HTML
        response.text = _html_page(
            DEVICE_NAME,
            (
                f"<h1>{escape(DEVICE_NAME)}</h1>"
                f"<p>{escape(DEVICE_DESCRIPTION)}</p>"
                "<p>This device is not configurable from this page. "
                "Use the local service configuration and restart the service.</p>"
                '<p><a href="/setup">Back to Alpaca setup</a></p>'
            ),
        )


def management_values(config: Any) -> tuple[dict[str, str], list[dict[str, object]]]:
    """Build immutable management values from approved metadata and config identity."""

    camera = getattr(config, "camera", None)
    serial = getattr(camera, "serial", None)
    source = getattr(camera, "source", None)
    description = {
        "ServerName": SERVER_NAME,
        "Manufacturer": MANUFACTURER,
        "ManufacturerVersion": DRIVER_VERSION,
        "Location": LOCATION,
    }
    configured_devices = [
        {
            "DeviceName": DEVICE_NAME,
            "DeviceType": DEVICE_TYPE,
            "DeviceNumber": DEVICE_NUMBER,
            "UniqueID": stable_unique_id(serial, source),
        }
    ]
    return description, configured_devices


def invalid_setup_path(response: falcon.Response) -> None:
    """Return the Management API's browser-facing unsupported-path response."""

    _unsupported_setup(response)


def _unsupported_setup(response: falcon.Response) -> None:
    response.status = falcon.HTTP_403
    response.content_type = falcon.MEDIA_HTML
    response.text = _html_page(
        "Unsupported setup path",
        "<h1>Unsupported setup path</h1>"
        "<p>The requested Alpaca device setup page is not available.</p>",
    )


def _html_page(title: str, body: str) -> str:
    safe_title = escape(title)
    return (
        "<!doctype html><html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        f"<title>{safe_title}</title>"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "</head><body>"
        f"{body}</body></html>"
    )
