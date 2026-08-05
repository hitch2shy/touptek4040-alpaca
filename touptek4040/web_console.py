"""Same-origin local Web console resources and scientific image services."""

from __future__ import annotations

import json
import math
import mimetypes
import queue
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.resources import files
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import falcon
import numpy as np

from .camera_contract import ExposureSettings, ReadoutMode, Roi
from .errors import (
    CameraBusyError,
    CameraError,
    CameraInvalidValueError,
    CameraNotConnectedError,
    safe_exception_text,
)
from .fits_io import stream_fits
from .image_workspace import ImageGeneration
from .png_stream import ViewportCrop, stream_viewport_png

WEB_CAPABILITIES = {
    "sensor": {
        "model": "sMAX16AM-U3-CL-GPS",
        "width": 4096,
        "height": 4096,
        "pixelMicrons": 9.0,
    },
    "readoutModes": ["RAW 12-bit", "RAW 8-bit"],
    "binning": list(range(1, 9)),
    "gain": {"min": 100, "max": 1650},
    "temperature": {"min": -50.0, "max": 40.0},
}

_MAX_JSON_BODY = 4096
_STATIC_ASSETS = {
    "index.html",
    "tokens.css",
    "app.css",
    "icons.svg",
    "api.js",
    "app.js",
    "image-viewer.js",
    "settings-sync.js",
    "log-view.js",
    "generation-contract.js",
}
_STATE_NAMES = {
    0: "idle",
    1: "waiting",
    2: "exposing",
    3: "reading",
    4: "download",
    5: "error",
}


class GenerationConflictError(Exception):
    """The requested immutable image generation is no longer current."""


@dataclass(frozen=True, slots=True)
class WebSettings:
    binning: int
    readout_mode: str
    gain: int
    roi: Roi


class LogBuffer:
    """Small bounded operational log shared by HTTP and the visible timeline."""

    def __init__(self, max_entries: int = 500) -> None:
        self._entries: deque[dict[str, object]] = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def append(self, level: str, message: str) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": level,
            "message": str(message)[:700],
        }
        with self._lock:
            self._entries.append(entry)

    def snapshot(self, level: str | None = None) -> list[dict[str, object]]:
        with self._lock:
            entries = list(self._entries)
        if level is None or level == "all":
            return entries
        return [entry for entry in entries if entry["level"] == level]


class WebConsole:
    def __init__(self, coordinator: Any, config: Any) -> None:
        self.coordinator = coordinator
        camera = getattr(config, "camera", SimpleNamespace())
        configured_gain = getattr(camera, "gain", None)
        configured_mode = getattr(camera, "readout_mode", "RAW 12-bit")
        self.serial = getattr(camera, "serial", None) or "Not selected"
        self._settings = WebSettings(
            binning=1,
            readout_mode=configured_mode,
            gain=100 if configured_gain is None else int(configured_gain),
            roi=Roi(0, 0, 4096, 4096),
        )
        self._settings_lock = threading.RLock()
        self._target_temperature_c = -10.0
        self._cooler_on = False
        self.logs = LogBuffer()
        self.logs.append(
            "warning",
            "Trusted LAN only: no TLS or account authentication is enabled.",
        )

    def settings(self) -> dict[str, object]:
        with self._settings_lock:
            settings = self._settings
            cached_target = self._target_temperature_c
            cached_cooler = self._cooler_on
        try:
            target_temperature = float(self.coordinator.target_temperature_c)
            cooler_on = bool(self.coordinator.cooler_on)
            with self._settings_lock:
                self._target_temperature_c = target_temperature
                self._cooler_on = cooler_on
        except Exception:
            target_temperature = cached_target
            cooler_on = cached_cooler
        return {
            **_settings_media(settings),
            "targetTemperature": target_temperature,
            "coolerOn": cooler_on,
        }

    def acquisition_settings(self) -> dict[str, object]:
        with self._settings_lock:
            return _settings_media(self._settings)

    def update_settings(self, payload: object) -> dict[str, object]:
        body = _object_with_keys(
            payload,
            {"binning", "readoutMode", "gain", "roi"},
            required={"binning", "readoutMode", "gain", "roi"},
        )
        binning = _strict_int(body["binning"], "binning", 1, 8)
        readout_mode = body["readoutMode"]
        if not isinstance(readout_mode, str) or readout_mode not in {
            "RAW 12-bit",
            "RAW 8-bit",
        }:
            raise ValueError("readoutMode is invalid")
        gain = _strict_int(body["gain"], "gain", 100, 1650)
        roi_body = _object_with_keys(
            body["roi"],
            {"x", "y", "width", "height"},
            required={"x", "y", "width", "height"},
        )
        roi = Roi(
            x=_strict_int(roi_body["x"], "roi.x", 0, 4095),
            y=_strict_int(roi_body["y"], "roi.y", 0, 4095),
            width=_strict_int(roi_body["width"], "roi.width", 1, 4096),
            height=_strict_int(roi_body["height"], "roi.height", 1, 4096),
        )
        candidate = WebSettings(binning, readout_mode, gain, roi)

        def commit(capabilities: Any) -> None:
            _validate_settings(candidate, capabilities)
            with self._settings_lock:
                self._settings = candidate

        self.coordinator.update_settings_if_idle(commit)
        self.logs.append("info", "Acquisition settings updated")
        return self.settings()

    def update_cooling(self, payload: object) -> dict[str, object]:
        body = _object_with_keys(
            payload,
            {"targetTemperature", "coolerOn"},
            required=set(),
        )
        if len(body) != 1:
            raise ValueError("Cooling requests must change exactly one field")
        if "targetTemperature" in body:
            target = _strict_number(
                body["targetTemperature"],
                "targetTemperature",
                -50.0,
                40.0,
            )
            self.coordinator.set_target_temperature_if_idle(target)
            with self._settings_lock:
                self._target_temperature_c = target
        else:
            cooler = body["coolerOn"]
            if not isinstance(cooler, bool):
                raise ValueError("coolerOn must be a boolean")
            self.coordinator.set_cooler_on_if_idle(cooler)
            with self._settings_lock:
                self._cooler_on = cooler
        self.logs.append("info", "Cooling setting updated")
        settings = self.settings()
        return {
            "targetTemperature": settings["targetTemperature"],
            "coolerOn": settings["coolerOn"],
        }

    def update_heater(self, payload: object) -> dict[str, object]:
        body = _object_with_keys(
            payload,
            {"heaterLevel"},
            required={"heaterLevel"},
        )
        if len(body) != 1:
            raise ValueError("Invalid request body")
        level = body["heaterLevel"]
        if isinstance(level, bool) or not isinstance(level, int):
            raise ValueError("Invalid request body")
        snapshot = self.coordinator.set_heater_level_confirmed_if_idle(level)
        confirmed = snapshot.level
        if (
            isinstance(confirmed, bool)
            or not isinstance(confirmed, int)
            or confirmed < 0
        ):
            raise CameraError("camera returned an invalid heater level")
        self.logs.append("info", f"Dew heater set to level {confirmed}")
        return {"heaterLevel": confirmed}

    def state(self) -> dict[str, object]:
        snapshot = self.coordinator.snapshot()
        generation = self.display_generation()
        acquisition_settings = self.acquisition_settings()
        try:
            preview_active = bool(self.coordinator.preview_active)
        except Exception:
            preview_active = False
        try:
            temperature = (
                float(self.coordinator.temperature_c)
                if snapshot.connected and not snapshot.connecting
                else None
            )
        except Exception:
            temperature = None
        try:
            cooler_power = (
                float(self.coordinator.cooler_power_percent)
                if snapshot.connected and not snapshot.connecting
                else None
            )
        except Exception:
            cooler_power = None
        state_number = int(snapshot.camera_state)
        heater_state = self._heater_state(
            connected=bool(snapshot.connected),
            connecting=bool(snapshot.connecting),
            state_number=state_number,
        )
        image = None
        if generation is not None:
            readout_mode = (
                "RAW 12-bit"
                if generation.frame.readout_mode is ReadoutMode.RAW12
                else "RAW 8-bit"
            )
            image = {
                "width": generation.width,
                "height": generation.height,
                "maxADU": 4095 if readout_mode == "RAW 12-bit" else 255,
                "readoutMode": readout_mode,
            }
        return {
            "model": "sMAX16AM-U3-CL-GPS",
            "serial": self.serial,
            "connected": bool(snapshot.connected),
            "connecting": bool(snapshot.connecting),
            "cameraState": _STATE_NAMES.get(state_number, "unknown"),
            "imageReady": bool(snapshot.image_ready),
            "percentCompleted": snapshot.percent_completed,
            "preview": preview_active,
            "generation": (
                generation.generation_id if generation is not None else None
            ),
            "image": image,
            "settings": acquisition_settings,
            "temperature": temperature,
            "coolerPower": cooler_power,
            **heater_state,
            "error": (
                safe_exception_text(snapshot.error)
                if snapshot.error is not None
                else None
            ),
        }

    def _heater_state(
        self,
        *,
        connected: bool,
        connecting: bool,
        state_number: int,
    ) -> dict[str, object]:
        if not connected or connecting:
            return _heater_media(False, None, None, False)

        try:
            snapshot = self.coordinator.heater_snapshot()
            if not isinstance(snapshot.supported, bool):
                raise CameraError("camera returned invalid heater support")
            if not snapshot.supported:
                return _heater_media(False, 0, None, False)
            maximum = snapshot.maximum
            if (
                isinstance(maximum, bool)
                or not isinstance(maximum, int)
                or maximum < 0
            ):
                raise CameraError("camera returned an invalid heater maximum")
            level = snapshot.level
            if (
                isinstance(level, bool)
                or not isinstance(level, int)
                or not 0 <= level <= maximum
            ):
                raise CameraError("camera returned an invalid heater level")
        except Exception:
            return _heater_media(False, None, None, False)

        return _heater_media(
            True,
            maximum,
            level,
            state_number == 0,
        )

    def display_generation(self) -> ImageGeneration | None:
        preview = getattr(self.coordinator, "preview_generation", None)
        if preview is not None:
            return preview
        return getattr(self.coordinator, "image_generation", None)

    def connect(self, connected: bool) -> None:
        if connected:
            self.coordinator.connect_async()
            self.logs.append("info", "Camera connection requested")
        else:
            self.coordinator.disconnect_async()
            self.logs.append("info", "Camera disconnect requested")

    def preview(self, enabled: bool) -> None:
        if enabled:
            self.coordinator.start_preview(lambda _frame: None)
            self.logs.append("info", "Preview started")
        else:
            self.coordinator.stop_preview()
            self.logs.append("info", "Preview stopped")

    def expose(self, duration: float) -> None:
        def snapshot(capabilities: Any) -> ExposureSettings:
            with self._settings_lock:
                settings = self._settings
            _validate_settings(settings, capabilities)
            return ExposureSettings(
                roi=settings.roi,
                binning=settings.binning,
                readout_mode=(
                    ReadoutMode.RAW12
                    if settings.readout_mode == "RAW 12-bit"
                    else ReadoutMode.RAW8
                ),
                gain=settings.gain,
            )

        self.coordinator.start_exposure(duration, True, settings=snapshot)
        self.logs.append("info", f"Exposure started ({duration:g} s)")

    def abort(self) -> None:
        self.coordinator.abort_exposure()
        self.logs.append("warning", "Abort requested")


class SecurityHeadersMiddleware:
    def process_response(
        self,
        request: falcon.Request,
        response: falcon.Response,
        resource: object,
        req_succeeded: bool,
    ) -> None:
        del request, resource, req_succeeded
        response.set_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        response.set_header("X-Content-Type-Options", "nosniff")
        response.set_header("Referrer-Policy", "no-referrer")
        response.set_header("X-Frame-Options", "DENY")
        response.set_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )


def register_web_console(
    app: falcon.App,
    coordinator: Any,
    config: Any,
) -> WebConsole:
    console = WebConsole(coordinator, config)
    app.add_route("/ui", StaticAssetResource("index.html"))
    app.add_route("/ui/", StaticAssetResource("index.html"))
    app.add_route("/ui/{asset}", StaticAssetResource())

    for command in ("state", "capabilities", "settings", "logs"):
        app.add_route(
            f"/web/v1/{command}",
            WebJsonResource(console, command),
        )
    for command in (
        "connection",
        "preview",
        "exposure",
        "abort",
        "cooling",
        "heater",
    ):
        app.add_route(
            f"/web/v1/{command}",
            WebMutationResource(console, command),
        )
    app.add_route("/web/v1/viewport.png", ViewportResource(console))
    app.add_route("/web/v1/statistics", StatisticsResource(console))
    app.add_route("/web/v1/pixel", PixelResource(console))
    app.add_route("/web/v1/image.fits", FitsResource(console))
    app.add_route("/web/v1/events", EventsResource(console))
    return console


class StaticAssetResource:
    def __init__(self, asset: str | None = None) -> None:
        self._asset = asset

    def on_get(
        self,
        request: falcon.Request,
        response: falcon.Response,
        asset: str | None = None,
    ) -> None:
        del request
        name = self._asset or asset
        if name not in _STATIC_ASSETS:
            raise falcon.HTTPNotFound()
        payload = files("touptek4040.web").joinpath(name).read_bytes()
        content_type = {
            ".html": "text/html",
            ".css": "text/css",
            ".js": "text/javascript",
            ".svg": "image/svg+xml",
        }.get(mimetypes.guess_extension(name), None)
        if name.endswith(".html"):
            content_type = "text/html"
        elif name.endswith(".css"):
            content_type = "text/css"
        elif name.endswith(".js"):
            content_type = "text/javascript"
        elif name.endswith(".svg"):
            content_type = "image/svg+xml"
        response.status = falcon.HTTP_200
        response.content_type = content_type or "application/octet-stream"
        if name.endswith((".html", ".css", ".js", ".svg")):
            response.set_header(
                "Content-Type",
                f"{content_type}; charset=utf-8",
            )
        response.set_header("Cache-Control", "no-cache")
        response.data = payload


class WebJsonResource:
    def __init__(self, console: WebConsole, command: str) -> None:
        self._console = console
        self._command = command

    def on_get(self, request: falcon.Request, response: falcon.Response) -> None:
        try:
            if self._command == "state":
                value: object = self._console.state()
            elif self._command == "capabilities":
                value = WEB_CAPABILITIES
            elif self._command == "settings":
                value = self._console.settings()
            else:
                level = request.get_param("level") or "all"
                if level not in {"all", "info", "warning", "error"}:
                    raise ValueError("level is invalid")
                value = {"entries": self._console.logs.snapshot(level)}
            response.media = value
            response.status = falcon.HTTP_200
        except BaseException as error:
            _web_error(response, error)

    def on_post(self, request: falcon.Request, response: falcon.Response) -> None:
        if self._command != "settings":
            raise falcon.HTTPMethodNotAllowed(("GET",))
        try:
            response.media = self._console.update_settings(
                _mutation_body(request)
            )
            response.status = falcon.HTTP_200
        except falcon.HTTPError:
            raise
        except BaseException as error:
            _web_error(response, error)


class WebMutationResource:
    def __init__(self, console: WebConsole, command: str) -> None:
        self._console = console
        self._command = command

    def on_post(self, request: falcon.Request, response: falcon.Response) -> None:
        try:
            body = _mutation_body(request)
            if self._command == "connection":
                payload = _object_with_keys(
                    body, {"connected"}, required={"connected"}
                )
                connected = payload["connected"]
                if not isinstance(connected, bool):
                    raise ValueError("connected must be a boolean")
                self._console.connect(connected)
                response.media = {"accepted": True}
                response.status = falcon.HTTP_202
            elif self._command == "preview":
                payload = _object_with_keys(
                    body, {"enabled"}, required={"enabled"}
                )
                enabled = payload["enabled"]
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be a boolean")
                self._console.preview(enabled)
                response.media = {"accepted": True}
                response.status = falcon.HTTP_202
            elif self._command == "exposure":
                payload = _object_with_keys(
                    body, {"duration"}, required={"duration"}
                )
                duration = _strict_number(
                    payload["duration"], "duration", 0.0001, 86400.0
                )
                self._console.expose(duration)
                response.media = {"accepted": True}
                response.status = falcon.HTTP_202
            elif self._command == "abort":
                _object_with_keys(body, set(), required=set())
                self._console.abort()
                response.media = {"accepted": True}
                response.status = falcon.HTTP_202
            elif self._command == "cooling":
                response.media = self._console.update_cooling(body)
                response.status = falcon.HTTP_200
            elif self._command == "heater":
                response.media = self._console.update_heater(body)
                response.status = falcon.HTTP_200
            else:
                response.media = self._console.update_settings(body)
                response.status = falcon.HTTP_200
        except falcon.HTTPError:
            raise
        except BaseException as error:
            _web_error(response, error)


class ViewportResource:
    def __init__(self, console: WebConsole) -> None:
        self._console = console

    def on_get(self, request: falcon.Request, response: falcon.Response) -> None:
        try:
            generation = _require_generation(self._console, request)
            crop = _viewport_crop(request, generation)
            max_adu = (
                4095
                if generation.frame.readout_mode is ReadoutMode.RAW12
                else 255
            )
            black = _query_int(request, "black", 0, 0, max_adu - 1)
            white = _query_int(
                request, "white", max_adu, black + 1, max_adu
            )
            gamma = _query_float(request, "gamma", 1.0, 0.1, 5.0)
            max_width = _query_int(request, "maxWidth", 1600, 1, 2048)
            max_height = _query_int(request, "maxHeight", 1200, 1, 2048)
            response.status = falcon.HTTP_200
            response.content_type = "image/png"
            response.set_header(
                "ETag",
                f'"generation-{generation.generation_id}'
                f'{_viewport_crop_etag(crop)}-b{black}-w{white}'
                f'-g{gamma:g}-{max_width}x{max_height}"',
            )
            response.set_header(
                "Cache-Control",
                "private, max-age=0, must-revalidate",
            )
            response.stream = stream_viewport_png(
                generation,
                black=black,
                white=white,
                gamma=gamma,
                max_width=max_width,
                max_height=max_height,
                crop=crop,
            )
        except BaseException as error:
            _web_error(response, error)


class StatisticsResource:
    def __init__(self, console: WebConsole) -> None:
        self._console = console

    def on_get(self, request: falcon.Request, response: falcon.Response) -> None:
        try:
            response.media = analyze_generation(
                _require_generation(self._console, request)
            )
            response.status = falcon.HTTP_200
        except BaseException as error:
            _web_error(response, error)


class PixelResource:
    def __init__(self, console: WebConsole) -> None:
        self._console = console

    def on_get(self, request: falcon.Request, response: falcon.Response) -> None:
        try:
            generation = _require_generation(self._console, request)
            x = _query_int(request, "x", None, 0, generation.width - 1)
            y = _query_int(request, "y", None, 0, generation.height - 1)
            response.media = {
                "generation": generation.generation_id,
                "x": x,
                "y": y,
                "value": int(generation.pixels[y, x]),
            }
            response.status = falcon.HTTP_200
        except BaseException as error:
            _web_error(response, error)


class FitsResource:
    def __init__(self, console: WebConsole) -> None:
        self._console = console

    def on_get(self, request: falcon.Request, response: falcon.Response) -> None:
        try:
            generation = _require_generation(self._console, request)
            response.status = falcon.HTTP_200
            response.content_type = "image/fits"
            response.set_header(
                "Content-Disposition",
                f'attachment; filename="touptek-generation-'
                f'{generation.generation_id}.fits"',
            )
            response.set_header(
                "Cache-Control",
                "private, max-age=0, must-revalidate",
            )
            response.stream = stream_fits(generation)
        except BaseException as error:
            _web_error(response, error)


class EventsResource:
    def __init__(self, console: WebConsole) -> None:
        self._console = console

    def on_get(self, request: falcon.Request, response: falcon.Response) -> None:
        del request
        response.status = falcon.HTTP_200
        response.content_type = "text/event-stream"
        response.set_header("Cache-Control", "no-cache")
        response.set_header("X-Accel-Buffering", "no")
        response.stream = _SseStream(self._console)


class _SseStream:
    def __init__(self, console: WebConsole, heartbeat_seconds: float = 15.0) -> None:
        self._console = console
        self._heartbeat_seconds = heartbeat_seconds
        self._closed = threading.Event()
        self._subscription: Any | None = None

    def __iter__(self):
        if self._closed.is_set():
            return
        subscription = self._console.coordinator.events.subscribe()
        self._subscription = subscription
        try:
            if self._closed.is_set():
                return
            yield _sse("state", self._console.state())
            while not self._closed.is_set():
                try:
                    subscription.get(timeout=self._heartbeat_seconds)
                except queue.Empty:
                    if self._closed.is_set():
                        break
                    yield b": heartbeat\n\n"
                    continue
                if self._closed.is_set():
                    break
                yield _sse("state", self._console.state())
        finally:
            subscription.close()
            self._subscription = None

    def close(self) -> None:
        self._closed.set()
        subscription = self._subscription
        if subscription is not None:
            subscription.close()


def analyze_generation(generation: ImageGeneration) -> dict[str, object]:
    max_adu = 4095 if generation.frame.readout_mode is ReadoutMode.RAW12 else 255
    histogram = np.zeros(256, dtype=np.int64)
    count = 0
    total = 0
    total_squares = 0
    minimum = max_adu
    maximum = 0
    peak_x = peak_y = 0
    for y0 in range(0, generation.height, 128):
        block = generation.pixels[y0 : y0 + 128]
        if block.size == 0:
            continue
        block_min = int(block.min())
        block_max = int(block.max())
        minimum = min(minimum, block_min)
        maximum = max(maximum, block_max)
        block64 = block.astype(np.uint64, copy=False)
        count += int(block.size)
        total += int(block64.sum(dtype=np.uint64))
        total_squares += int((block64 * block64).sum(dtype=np.uint64))
        bins = ((block64 * 255) // max_adu).astype(np.uint8)
        histogram += np.bincount(bins.ravel(), minlength=256)
        local_index = int(np.argmax(block))
        local_y, local_x = np.unravel_index(local_index, block.shape)
        if int(block[local_y, local_x]) > int(
            generation.pixels[peak_y, peak_x]
        ):
            peak_y = y0 + int(local_y)
            peak_x = int(local_x)
    mean = total / count if count else 0.0
    variance = max(0.0, total_squares / count - mean * mean) if count else 0.0
    return {
        "generation": generation.generation_id,
        "count": count,
        "minimum": minimum if count else None,
        "maximum": maximum if count else None,
        "mean": round(mean, 6),
        "standardDeviation": round(math.sqrt(variance), 6),
        "histogram": [int(value) for value in histogram],
        "peak": {
            "x": peak_x,
            "y": peak_y,
            "value": int(generation.pixels[peak_y, peak_x]),
        },
        "fwhm": {
            "x": _fwhm(generation.pixels[peak_y, :], peak_x, minimum),
            "y": _fwhm(generation.pixels[:, peak_x], peak_y, minimum),
        },
    }


def _fwhm(profile: np.ndarray, peak_index: int, baseline: int) -> float | None:
    peak = float(profile[peak_index])
    half = float(baseline) + (peak - float(baseline)) / 2.0
    left = peak_index
    while left > 0 and float(profile[left - 1]) >= half:
        left -= 1
    right = peak_index
    while right + 1 < profile.size and float(profile[right + 1]) >= half:
        right += 1
    return float(right - left + 1)


def _mutation_body(request: falcon.Request) -> object:
    origin = request.get_header("Origin")
    parsed = urlsplit(origin) if origin and origin != "null" else None
    if (
        parsed is None
        or parsed.scheme not in {"http", "https"}
        or parsed.netloc.casefold() != request.netloc.casefold()
        or request.get_header("X-ToupTek-Console") != "1"
        or request.content_type != "application/json"
    ):
        raise falcon.HTTPForbidden(
            title="Forbidden",
            description="Same-origin console JSON request required",
        )
    if request.content_length is not None and request.content_length > _MAX_JSON_BODY:
        raise falcon.HTTPContentTooLarge(
            title="Request too large",
            description="JSON body exceeds 4096 bytes",
        )
    raw = request.bounded_stream.read(_MAX_JSON_BODY + 1)
    if len(raw) > _MAX_JSON_BODY:
        raise falcon.HTTPContentTooLarge(
            title="Request too large",
            description="JSON body exceeds 4096 bytes",
        )
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("Invalid request body") from error


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _object_with_keys(
    payload: object,
    allowed: set[str],
    *,
    required: set[str],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Invalid request body")
    if any(not isinstance(key, str) for key in payload):
        raise ValueError("Invalid request body")
    if set(payload) - allowed or not required.issubset(payload):
        raise ValueError("Invalid request body")
    return payload


def _strict_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _strict_number(
    value: object,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid request body")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError("Invalid request body")
    return result


def _validate_settings(settings: WebSettings, capabilities: Any) -> None:
    roi = settings.roi
    scale = settings.binning
    valid = (
        settings.binning <= capabilities.max_bin
        and settings.readout_mode in capabilities.readout_modes
        and (roi.x + roi.width) * scale <= capabilities.width
        and (roi.y + roi.height) * scale <= capabilities.height
        and roi.x * scale % 2 == 0
        and roi.y * scale % 2 == 0
        and roi.width * scale >= 16
        and roi.height * scale >= 16
        and roi.width * scale % 2 == 0
        and roi.height * scale % 2 == 0
    )
    if not valid:
        raise ValueError("ROI and binning combination is invalid")


def _settings_media(settings: WebSettings) -> dict[str, object]:
    return {
        "binning": settings.binning,
        "readoutMode": settings.readout_mode,
        "gain": settings.gain,
        "roi": asdict(settings.roi),
    }


def _heater_media(
    supported: bool,
    maximum: int | None,
    level: int | None,
    writable: bool,
) -> dict[str, object]:
    return {
        "heaterSupported": supported,
        "heaterLevel": level,
        "heaterMaxLevel": maximum,
        "heaterWritable": writable,
    }


def _require_generation(
    console: WebConsole,
    request: falcon.Request,
) -> ImageGeneration:
    requested = _query_int(request, "generation", None, 1, 2_147_483_647)
    generation = console.display_generation()
    if generation is None:
        raise LookupError("No image generation is available")
    if generation.generation_id != requested:
        raise GenerationConflictError("Image generation changed")
    return generation


def _viewport_crop(
    request: falcon.Request,
    generation: ImageGeneration,
) -> ViewportCrop | None:
    names = ("x", "y", "width", "height")
    present = tuple(request.get_param(name) is not None for name in names)
    if not any(present):
        return None
    if not all(present):
        raise ValueError("viewport crop requires x, y, width and height")
    x = _query_int(request, "x", None, 0, generation.width - 1)
    y = _query_int(request, "y", None, 0, generation.height - 1)
    width = _query_int(request, "width", None, 1, generation.width - x)
    height = _query_int(request, "height", None, 1, generation.height - y)
    return ViewportCrop(x, y, width, height)


def _viewport_crop_etag(crop: ViewportCrop | None) -> str:
    if crop is None:
        return ""
    return f"-c{crop.x}-{crop.y}-{crop.width}x{crop.height}"


def _query_int(
    request: falcon.Request,
    name: str,
    default: int | None,
    minimum: int,
    maximum: int,
) -> int:
    value = request.get_param(name)
    if value is None:
        if default is None:
            raise ValueError(f"{name} is required")
        return default
    if len(value) > 10 or not value.isascii() or not value.isdecimal():
        raise ValueError(f"{name} is invalid")
    return _strict_int(int(value), name, minimum, maximum)


def _query_float(
    request: falcon.Request,
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    value = request.get_param(name)
    if value is None:
        return default
    if len(value) > 32:
        raise ValueError(f"{name} is invalid")
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{name} is invalid") from error
    return _strict_number(parsed, name, minimum, maximum)


def _sse(event: str, payload: object) -> bytes:
    data = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")


def _web_error(response: falcon.Response, error: BaseException) -> None:
    if isinstance(error, GenerationConflictError):
        status = falcon.HTTP_409
        message = "Image generation changed"
    elif isinstance(error, LookupError):
        status = falcon.HTTP_404
        message = safe_exception_text(error)
    elif isinstance(error, (ValueError, CameraInvalidValueError)):
        status = falcon.HTTP_400
        message = (
            "Invalid request body"
            if isinstance(error, ValueError)
            else safe_exception_text(error)
        )
    elif isinstance(error, CameraBusyError):
        status = falcon.HTTP_409
        message = safe_exception_text(error)
    elif isinstance(error, CameraNotConnectedError):
        status = falcon.HTTP_503
        message = safe_exception_text(error)
    elif isinstance(error, CameraError):
        status = falcon.HTTP_500
        message = safe_exception_text(error)
    else:
        status = falcon.HTTP_500
        message = "Camera console operation failed"
    response.status = status
    response.content_type = falcon.MEDIA_JSON
    response.media = {"error": message}
