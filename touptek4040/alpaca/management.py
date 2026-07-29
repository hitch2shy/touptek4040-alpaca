"""Official Alpaca management JSON and read-only setup resources."""

from __future__ import annotations

from collections.abc import Mapping
import configparser
from dataclasses import dataclass
from html import escape
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from types import SimpleNamespace
from typing import Any, Callable
import uuid

import falcon

from .. import __version__
from ..config import load_json_object_strict
from .common_api import (
    DEVICE_DESCRIPTION,
    DEVICE_NAME,
)
from .responses import (
    INVALID_VALUE,
    AlpacaError,
    RequestParseError,
    ServerTransactionIdAllocator,
    property_response,
)

SERVER_NAME = "ToupTek 4040 Alpaca Server"
MANUFACTURER = "Hangzhou ToupTek Photonics Co., Ltd."
MANUFACTURER_VERSION = __version__
LOCATION = "Local"
DEVICE_TYPE = "Camera"
DEVICE_NUMBER = 0
SIMULATOR_SERIAL = "SIM-4040"

_UID_PREFIX = "touptek4040:camera:"
_UINT32_MAX = 0xFFFFFFFF
_MANAGEMENT_QUERY_ERROR = "Invalid management query parameters"


class _ConfigurationTargetUnavailable(Exception):
    pass


@dataclass(frozen=True)
class _ConfigurationIdentity:
    target: Path
    device: int
    inode: int


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
            client_id = management_client_transaction_id(request)
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


def management_client_transaction_id(request: Any) -> int:
    """Validate the two optional Management API uint32 query parameters."""

    try:
        params = request.params
        if not isinstance(params, Mapping):
            raise TypeError("query parameters must be a mapping")
        recognized: dict[str, list[object]] = {
            "clientid": [],
            "clienttransactionid": [],
        }
        for key, raw_value in params.items():
            if not isinstance(key, str):
                raise TypeError("query parameter names must be strings")
            folded = key.casefold()
            if folded not in recognized:
                continue
            if isinstance(raw_value, (list, tuple)):
                recognized[folded].extend(raw_value)
            else:
                recognized[folded].append(raw_value)
        _management_uint32(recognized["clientid"])
        return _management_uint32(recognized["clienttransactionid"])
    except RequestParseError:
        raise
    except BaseException as error:
        raise RequestParseError(
            INVALID_VALUE,
            _MANAGEMENT_QUERY_ERROR,
        ) from error


def _management_uint32(values: list[object]) -> int:
    if not values:
        return 0
    parsed = [_parse_management_uint32(value) for value in values]
    if any(value != parsed[0] for value in parsed[1:]):
        raise RequestParseError(INVALID_VALUE, _MANAGEMENT_QUERY_ERROR)
    return parsed[0]


def _parse_management_uint32(value: object) -> int:
    if isinstance(value, bool):
        raise RequestParseError(INVALID_VALUE, _MANAGEMENT_QUERY_ERROR)
    if isinstance(value, int):
        parsed = value
    elif (
        isinstance(value, str)
        and 1 <= len(value) <= 10
        and value.isascii()
        and value.isdecimal()
    ):
        parsed = int(value)
    else:
        raise RequestParseError(INVALID_VALUE, _MANAGEMENT_QUERY_ERROR)
    if not 0 <= parsed <= _UINT32_MAX:
        raise RequestParseError(INVALID_VALUE, _MANAGEMENT_QUERY_ERROR)
    return parsed


class MainSetupResource:
    """Safe server-listener configuration page when a config path is explicit."""

    def __init__(self, config: Any) -> None:
        self._config = config
        self._config_path = _explicit_config_path(
            getattr(config, "config_path", None)
        )
        self._approved_identity = _resolve_file_identity(self._config_path)
        self._operation_lock = threading.Lock()

    def on_get(self, request: falcon.Request, response: falcon.Response) -> None:
        with self._operation_lock:
            self._respond_to_get(request, response)

    def _respond_to_get(
        self,
        request: falcon.Request,
        response: falcon.Response,
    ) -> None:
        del request
        server = getattr(self._config, "server", None)
        identity = self._current_write_identity()
        _setup_response(
            response,
            falcon.HTTP_200,
            _main_setup_body(
                identity.target if identity is not None else None,
                getattr(server, "host", ""),
                getattr(server, "port", ""),
                getattr(server, "discovery_port", ""),
            ),
        )

    def on_post(self, request: falcon.Request, response: falcon.Response) -> None:
        with self._operation_lock:
            self._respond_to_post(request, response)

    def _respond_to_post(
        self,
        request: falcon.Request,
        response: falcon.Response,
    ) -> None:
        if self._config_path is None:
            _setup_response(
                response,
                falcon.HTTP_403,
                "<h1>Read-only setup</h1>"
                "<p>This installation has no explicit configuration file, "
                "so server settings are read-only.</p>",
            )
            return
        identity = self._current_write_identity()
        if identity is None:
            _configuration_unavailable_response(response)
            return
        try:
            values = _server_form_values(request)
        except (RequestParseError, falcon.HTTPError):
            _setup_response(
                response,
                falcon.HTTP_400,
                "<h1>Invalid server configuration</h1>"
                "<p>Provide a valid listen host and TCP/UDP ports.</p>",
            )
            return
        try:
            new_identity = _write_server_config(
                self._config_path,
                identity,
                values,
            )
        except _ConfigurationTargetUnavailable:
            _configuration_unavailable_response(response)
            return
        except OSError:
            _setup_response(
                response,
                falcon.HTTP_500,
                "<h1>Configuration could not be saved</h1>"
                "<p>The existing configuration was left unchanged.</p>",
            )
            return
        self._approved_identity = new_identity
        _setup_response(
            response,
            falcon.HTTP_200,
            _main_setup_body(
                new_identity.target,
                values["host"],
                values["port"],
                values["discovery_port"],
                notice=(
                    "<p>Configuration saved. Restart the service for the "
                    "new listener settings to take effect.</p>"
                ),
            ),
        )

    def _current_write_identity(self) -> _ConfigurationIdentity | None:
        identity = _resolve_file_identity(self._config_path)
        if identity is None or identity != self._approved_identity:
            return None
        return identity


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
        "ManufacturerVersion": MANUFACTURER_VERSION,
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


def _server_form_values(request: falcon.Request) -> dict[str, str]:
    content_type = request.content_type or ""
    if content_type.partition(";")[0].strip().casefold() != (
        "application/x-www-form-urlencoded"
    ):
        raise RequestParseError(INVALID_VALUE, "Invalid server configuration")
    media = request.media
    if not isinstance(media, Mapping):
        raise RequestParseError(INVALID_VALUE, "Invalid server configuration")
    values = {
        name: _single_form_value(media, name)
        for name in ("host", "port", "discovery_port")
    }
    _validate_host(values["host"])
    _validate_port(values["port"])
    _validate_port(values["discovery_port"])
    return values


def _single_form_value(media: Mapping[object, object], name: str) -> str:
    raw = media.get(name)
    candidates = list(raw) if isinstance(raw, (list, tuple)) else [raw]
    if (
        not candidates
        or any(not isinstance(value, str) for value in candidates)
        or any(value != candidates[0] for value in candidates[1:])
    ):
        raise RequestParseError(INVALID_VALUE, "Invalid server configuration")
    return candidates[0]


def _validate_host(host: str) -> None:
    if (
        not host
        or len(host) > 253
        or not host.isascii()
        or host != host.strip()
    ):
        raise RequestParseError(INVALID_VALUE, "Invalid server configuration")
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    labels = host.removesuffix(".").split(".")
    if not labels or any(
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in labels
    ):
        raise RequestParseError(INVALID_VALUE, "Invalid server configuration")


def _validate_port(port: str) -> None:
    if (
        not 1 <= len(port) <= 5
        or not port.isascii()
        or not port.isdecimal()
        or not 1 <= int(port) <= 65535
    ):
        raise RequestParseError(INVALID_VALUE, "Invalid server configuration")


def _write_server_config(
    config_path: Path,
    expected: _ConfigurationIdentity,
    values: Mapping[str, str],
) -> _ConfigurationIdentity:
    _require_config_identity(config_path, expected)
    try:
        with expected.target.open("r", encoding="utf-8") as stream:
            _require_open_file_identity(stream, expected)
            if expected.target.suffix == ".json":
                document = load_json_object_strict(expected.target)
                server = document.get("server")
                if not isinstance(server, dict):
                    raise ValueError("JSON configuration server must be an object")
                server.update(
                    host=values["host"],
                    port=int(values["port"]),
                    discovery_port=int(values["discovery_port"]),
                )
                rendered = json.dumps(document, indent=2) + "\n"
            elif expected.target.suffix == ".ini":
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_file(stream)
                if not parser.has_section("server"):
                    parser.add_section("server")
                for name in ("host", "port", "discovery_port"):
                    parser.set("server", name, values[name])
                from io import StringIO

                output = StringIO()
                parser.write(output)
                rendered = output.getvalue()
            else:
                raise ValueError("unsupported configuration suffix")
    except OSError as error:
        raise _ConfigurationTargetUnavailable from error
    except (ValueError, configparser.Error) as error:
        raise OSError("configuration could not be read") from error

    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{expected.target.name}.",
            suffix=".tmp",
            dir=expected.target.parent,
        )
        temporary_path = Path(raw_path)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        replacement_stat = temporary_path.stat()
        replacement_key = (
            replacement_stat.st_dev,
            replacement_stat.st_ino,
        )
        # Recheck after the durable temporary file has been produced.
        _require_config_identity(config_path, expected)
        # Keep the final identity check immediately adjacent to replacement.
        _require_config_identity(config_path, expected)
        os.replace(temporary_path, expected.target)
        temporary_path = None
        new_identity = _require_regular_file(config_path)
        if (new_identity.device, new_identity.inode) != replacement_key:
            raise _ConfigurationTargetUnavailable
        return new_identity
    except (OSError, _ConfigurationTargetUnavailable):
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _explicit_config_path(value: object) -> Path | None:
    if value is None or value == "":
        return None
    try:
        return Path(value)
    except (OSError, TypeError, ValueError):
        return None


def _resolve_file_identity(
    config_path: Path | None,
) -> _ConfigurationIdentity | None:
    if config_path is None:
        return None
    try:
        return _require_regular_file(config_path)
    except (OSError, RuntimeError, _ConfigurationTargetUnavailable):
        return None


def _require_regular_file(config_path: Path) -> _ConfigurationIdentity:
    target = config_path.resolve(strict=True)
    metadata = target.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_ino == 0:
        raise _ConfigurationTargetUnavailable
    return _ConfigurationIdentity(
        target,
        metadata.st_dev,
        metadata.st_ino,
    )


def _require_config_identity(
    config_path: Path,
    expected: _ConfigurationIdentity,
) -> None:
    try:
        current = _require_regular_file(config_path)
    except (OSError, RuntimeError) as error:
        raise _ConfigurationTargetUnavailable from error
    if current != expected:
        raise _ConfigurationTargetUnavailable


def _require_open_file_identity(
    stream: Any,
    expected: _ConfigurationIdentity,
) -> None:
    metadata = os.fstat(stream.fileno())
    if (metadata.st_dev, metadata.st_ino) != (
        expected.device,
        expected.inode,
    ):
        raise _ConfigurationTargetUnavailable


def _main_setup_body(
    config_path: Path | None,
    host: object,
    port: object,
    discovery_port: object,
    *,
    notice: str = "",
) -> str:
    information = (
        f"<h1>{escape(SERVER_NAME)}</h1>"
        f"<p>Manufacturer: {escape(MANUFACTURER)}</p>"
        f"<p>Version: {escape(MANUFACTURER_VERSION)}</p>"
        f"<p>Location: {escape(LOCATION)}</p>"
    )
    if config_path is None:
        settings = (
            "<p>This is a read-only information page. "
            "No hardware or service settings are changed here.</p>"
        )
    else:
        settings = (
            f"{notice}"
            '<form method="post" action="/setup">'
            '<label>Listen host <input name="host" required value="'
            f'{escape(str(host), quote=True)}"></label>'
            '<label>Server port <input name="port" inputmode="numeric" '
            f'required value="{escape(str(port), quote=True)}"></label>'
            '<label>Discovery port <input name="discovery_port" '
            'inputmode="numeric" required value="'
            f'{escape(str(discovery_port), quote=True)}"></label>'
            "<button type=\"submit\">Save server settings</button>"
            "</form>"
            "<p>Changes require a service restart.</p>"
        )
    return (
        f"{information}{settings}"
        '<p><a href="/setup/v1/camera/0/setup">Camera 0 information</a></p>'
    )


def _setup_response(
    response: falcon.Response,
    status: str,
    body: str,
) -> None:
    response.status = status
    response.content_type = falcon.MEDIA_HTML
    response.text = _html_page(SERVER_NAME, body)


def _configuration_unavailable_response(response: falcon.Response) -> None:
    _setup_response(
        response,
        falcon.HTTP_403,
        "<h1>Configuration file is unavailable</h1>"
        "<p>Server settings are read-only because the configured path "
        "does not resolve to the approved existing file.</p>",
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
