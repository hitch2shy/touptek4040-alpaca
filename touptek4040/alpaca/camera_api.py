"""ASCOM Alpaca Camera V4 properties, settings, and control methods."""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from typing import Any

from ..camera_contract import CameraCapabilities
from ..camera_coordinator import CameraCoordinator, CameraState
from .common_api import _alpaca_bool
from .request import get_parameter
from .responses import (
    INVALID_OPERATION,
    INVALID_VALUE,
    METHOD_NOT_IMPLEMENTED,
    VALUE_NOT_SET,
    AlpacaError,
    RequestParseError,
)

_SENSOR_NAME = "sMAX16AM-U3-CL-GPS"
_GAIN_MIN = 100
_GAIN_MAX = 1650
_EXPOSURE_MIN = 0.000012
_EXPOSURE_MAX = 3600.0
_EXPOSURE_RESOLUTION = 0.000001
_TEMPERATURE_MIN = -50.0
_TEMPERATURE_MAX = 40.0
_ALPACA_FLOAT = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?",
    re.ASCII,
)

_UNSUPPORTED = frozenset(
    {
        "bayeroffsetx",
        "bayeroffsety",
        "electronsperadu",
        "fastreadout",
        "fullwellcapacity",
        "gains",
        "heatsinktemperature",
        "offset",
        "offsetmax",
        "offsetmin",
        "offsets",
        "subexposureduration",
    }
)


@dataclass
class _CameraSettings:
    binning: int = 1
    start_x: int = 0
    start_y: int = 0
    num_x: int = 4096
    num_y: int = 4096
    gain: int = _GAIN_MIN
    readout_mode: int = 0


class CameraApi:
    """Implement Camera V4 behavior independently of Falcon routing."""

    def __init__(self, coordinator: CameraCoordinator, config: Any) -> None:
        self._coordinator = coordinator
        self._lock = threading.RLock()
        camera_config = getattr(config, "camera", None)
        configured_gain = getattr(camera_config, "gain", None)
        configured_mode = getattr(camera_config, "readout_mode", "RAW 12-bit")
        gain = _GAIN_MIN if configured_gain is None else _strict_int(
            configured_gain, "Gain"
        )
        if not _GAIN_MIN <= gain <= _GAIN_MAX:
            raise ValueError(f"configured gain must be {_GAIN_MIN}..{_GAIN_MAX}")
        if configured_mode not in {"RAW 12-bit", "RAW 8-bit"}:
            raise ValueError("configured readout mode must be RAW 12-bit or RAW 8-bit")
        self._settings = _CameraSettings(
            gain=gain,
            readout_mode=0 if configured_mode == "RAW 12-bit" else 1,
        )

    def get(self, command: str, request: Any) -> object:
        del request
        if command in _UNSUPPORTED:
            self._not_implemented(command)

        snapshot = self._coordinator.snapshot()
        if command == "camerastate":
            return int(snapshot.camera_state)
        if command == "imageready":
            return snapshot.image_ready
        if command == "percentcompleted":
            if snapshot.percent_completed is None:
                raise AlpacaError(
                    INVALID_OPERATION,
                    "PercentCompleted is unavailable outside an exposure",
                )
            return snapshot.percent_completed
        if command == "lastexposureduration":
            frame = self._coordinator.frame
            if frame is None:
                raise AlpacaError(VALUE_NOT_SET, "No completed exposure is available")
            return frame.duration_s
        if command == "lastexposurestarttime":
            frame = self._coordinator.frame
            if frame is None:
                raise AlpacaError(VALUE_NOT_SET, "No completed exposure is available")
            return frame.started_utc.isoformat().replace("+00:00", "Z")
        if command == "canasymmetricbin":
            return False
        if command == "canfastreadout":
            return False
        if command == "cangetcoolerpower":
            return True
        if command == "canpulseguide":
            return False
        if command == "cansetccdtemperature":
            return True
        if command == "hasshutter":
            return False
        if command == "ispulseguiding":
            return False
        if command == "exposuremin":
            return _EXPOSURE_MIN
        if command == "exposuremax":
            return _EXPOSURE_MAX
        if command == "exposureresolution":
            return _EXPOSURE_RESOLUTION
        if command == "sensorname":
            return _SENSOR_NAME
        if command == "sensortype":
            return 0

        self._require_connected()
        capabilities = self._coordinator.capabilities
        if command == "cameraxsize":
            return capabilities.width
        if command == "cameraysize":
            return capabilities.height
        if command == "pixelsizex":
            return capabilities.pixel_size_x
        if command == "pixelsizey":
            return capabilities.pixel_size_y
        if command == "canabortexposure":
            return capabilities.can_abort
        if command == "canstopexposure":
            return capabilities.can_stop
        if command == "maxbinx" or command == "maxbiny":
            return capabilities.max_bin
        if command == "readoutmodes":
            return list(capabilities.readout_modes)
        if command == "ccdtemperature":
            return self._coordinator.temperature_c
        if command == "setccdtemperature":
            return self._coordinator.target_temperature_c
        if command == "cooleron":
            return self._coordinator.cooler_on
        if command == "coolerpower":
            current, maximum = self._coordinator.tec_voltage()
            if not math.isfinite(current) or not math.isfinite(maximum) or maximum <= 0:
                raise AlpacaError(
                    INVALID_OPERATION,
                    "TEC maximum voltage is unavailable",
                )
            return min(100.0, max(0.0, current * 100.0 / maximum))

        with self._lock:
            if command == "binx" or command == "biny":
                return self._settings.binning
            if command == "startx":
                return self._settings.start_x
            if command == "starty":
                return self._settings.start_y
            if command == "numx":
                return self._settings.num_x
            if command == "numy":
                return self._settings.num_y
            if command == "gain":
                return self._settings.gain
            if command == "gainmin":
                return _GAIN_MIN
            if command == "gainmax":
                return _GAIN_MAX
            if command == "readoutmode":
                return self._settings.readout_mode
            if command == "maxadu":
                return 4095 if self._settings.readout_mode == 0 else 255
        self._not_implemented(command)

    def put(self, command: str, request: Any) -> None:
        if command in _UNSUPPORTED or command == "pulseguide":
            self._not_implemented(command)
        if command == "abortexposure":
            self._coordinator.abort_exposure()
            return
        if command == "startexposure":
            self._start_exposure(request)
            return
        if command == "stopexposure":
            self._stop_exposure()
            return

        self._require_connected()
        self._require_idle()
        if command == "binx" or command == "biny":
            value = _strict_int(get_parameter(request, command), command)

            def commit_binning(capabilities):
                _require_int_range(value, command, 1, capabilities.max_bin)
                with self._lock:
                    self._settings.binning = value

            self._coordinator.update_settings_if_idle(commit_binning)
            return
        if command == "startx":
            value = _strict_int(get_parameter(request, "StartX"), "StartX")

            def commit_start_x(capabilities):
                _require_int_range(value, "StartX", 0, capabilities.width - 1)
                with self._lock:
                    self._settings.start_x = value

            self._coordinator.update_settings_if_idle(commit_start_x)
            return
        if command == "starty":
            value = _strict_int(get_parameter(request, "StartY"), "StartY")

            def commit_start_y(capabilities):
                _require_int_range(value, "StartY", 0, capabilities.height - 1)
                with self._lock:
                    self._settings.start_y = value

            self._coordinator.update_settings_if_idle(commit_start_y)
            return
        if command == "numx":
            value = _strict_int(get_parameter(request, "NumX"), "NumX")

            def commit_num_x(capabilities):
                _require_int_range(value, "NumX", 1, capabilities.width)
                with self._lock:
                    self._settings.num_x = value

            self._coordinator.update_settings_if_idle(commit_num_x)
            return
        if command == "numy":
            value = _strict_int(get_parameter(request, "NumY"), "NumY")

            def commit_num_y(capabilities):
                _require_int_range(value, "NumY", 1, capabilities.height)
                with self._lock:
                    self._settings.num_y = value

            self._coordinator.update_settings_if_idle(commit_num_y)
            return
        if command == "gain":
            value = _strict_int(get_parameter(request, "Gain"), "Gain")

            def commit_gain(capabilities):
                del capabilities
                _require_int_range(value, "Gain", _GAIN_MIN, _GAIN_MAX)
                with self._lock:
                    self._settings.gain = value

            self._coordinator.update_settings_if_idle(commit_gain)
            return
        if command == "readoutmode":
            value = _strict_int(
                get_parameter(request, "ReadoutMode"),
                "ReadoutMode",
            )

            def commit_readout_mode(capabilities):
                _require_int_range(
                    value,
                    "ReadoutMode",
                    0,
                    len(capabilities.readout_modes) - 1,
                )
                with self._lock:
                    self._settings.readout_mode = value

            self._coordinator.update_settings_if_idle(commit_readout_mode)
            return
        if command == "setccdtemperature":
            target = _strict_float(
                get_parameter(request, "SetCCDTemperature"),
                "SetCCDTemperature",
            )
            if not _TEMPERATURE_MIN <= target <= _TEMPERATURE_MAX:
                raise AlpacaError(
                    INVALID_VALUE,
                    "SetCCDTemperature must be between -50.0 and 40.0",
                )
            self._coordinator.set_target_temperature_if_idle(target)
            return
        if command == "cooleron":
            enabled = _alpaca_bool(
                get_parameter(request, "CoolerOn"),
                "CoolerOn",
            )
            self._coordinator.set_cooler_on_if_idle(enabled)
            return
        self._not_implemented(command)

    def device_state(self) -> list[dict[str, object]]:
        snapshot = self._coordinator.snapshot()
        state: list[dict[str, object]] = [
            {"Name": "CameraState", "Value": int(snapshot.camera_state)},
            {"Name": "ImageReady", "Value": snapshot.image_ready},
            {"Name": "PercentCompleted", "Value": snapshot.percent_completed},
        ]
        if snapshot.connected and not snapshot.connecting:
            state.extend(
                (
                    {"Name": "CCDTemperature", "Value": self._coordinator.temperature_c},
                    {"Name": "CoolerOn", "Value": self._coordinator.cooler_on},
                    {"Name": "CoolerPower", "Value": self.get("coolerpower", None)},
                )
            )
        return state

    def _start_exposure(self, request: Any) -> None:
        self._require_connected()
        self._require_idle()
        duration = _strict_float(get_parameter(request, "Duration"), "Duration")
        light = _alpaca_bool(get_parameter(request, "Light"), "Light")
        exposure_us = round(duration * 1_000_000)
        if not 12 <= exposure_us <= 3_600_000_000:
            raise AlpacaError(
                INVALID_VALUE,
                "Duration must quantize to between 12 and 3600000000 microseconds",
            )
        self._coordinator.start_exposure(
            exposure_us / 1_000_000,
            light,
            validate=self._validate_roi_combination,
        )

    def _validate_roi_combination(
        self,
        capabilities: CameraCapabilities,
    ) -> None:
        with self._lock:
            settings = _CameraSettings(**vars(self._settings))
        checks = (
            (
                (settings.start_x + settings.num_x) * settings.binning
                <= capabilities.width,
                "NumX",
                "horizontal ROI extends outside the sensor",
            ),
            (
                (settings.start_y + settings.num_y) * settings.binning
                <= capabilities.height,
                "NumY",
                "vertical ROI extends outside the sensor",
            ),
            (
                settings.start_x * settings.binning % 2 == 0,
                "StartX",
                "SDK coordinate must be even",
            ),
            (
                settings.start_y * settings.binning % 2 == 0,
                "StartY",
                "SDK coordinate must be even",
            ),
            (
                settings.num_x * settings.binning >= 16
                and settings.num_x * settings.binning % 2 == 0,
                "NumX",
                "SDK width must be even and at least 16",
            ),
            (
                settings.num_y * settings.binning >= 16
                and settings.num_y * settings.binning % 2 == 0,
                "NumY",
                "SDK height must be even and at least 16",
            ),
        )
        for valid, field, reason in checks:
            if not valid:
                raise AlpacaError(INVALID_VALUE, f"{field} is invalid: {reason}")

    def _stop_exposure(self) -> None:
        self._require_connected()
        state = self._coordinator.snapshot().camera_state
        if state is CameraState.IDLE:
            return
        if state is CameraState.EXPOSING:
            raise AlpacaError(
                METHOD_NOT_IMPLEMENTED,
                "StopExposure is not supported while exposing; use AbortExposure",
            )
        raise AlpacaError(
            INVALID_OPERATION,
            f"StopExposure is invalid while camera state is {state.name}",
        )

    def _require_connected(self) -> None:
        snapshot = self._coordinator.snapshot()
        if not snapshot.connected:
            from .responses import NOT_CONNECTED

            raise AlpacaError(NOT_CONNECTED, "Camera is not connected")
        if snapshot.connecting:
            raise AlpacaError(
                INVALID_OPERATION,
                "Camera connection is changing",
            )

    def _require_idle(self) -> None:
        state = self._coordinator.snapshot().camera_state
        if state is not CameraState.IDLE:
            raise AlpacaError(
                INVALID_OPERATION,
                f"Camera setting cannot change while state is {state.name}",
            )

    @staticmethod
    def _not_implemented(command: str) -> None:
        raise AlpacaError(
            METHOD_NOT_IMPLEMENTED,
            f"{command} is not implemented by this camera",
        )


def _strict_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise RequestParseError(INVALID_VALUE, f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if (
            stripped
            and stripped.isascii()
            and (
                stripped.isdecimal()
                or (
                    stripped[0] in {"+", "-"}
                    and len(stripped) > 1
                    and stripped[1:].isdecimal()
                )
            )
        ):
            try:
                return int(stripped)
            except (ValueError, OverflowError):
                pass
    raise RequestParseError(INVALID_VALUE, f"{name} must be an integer")


def _require_int_range(
    parsed: int,
    name: str,
    minimum: int,
    maximum: int,
) -> None:
    if not minimum <= parsed <= maximum:
        raise AlpacaError(
            INVALID_VALUE,
            f"{name} must be between {minimum} and {maximum}",
        )


def _strict_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RequestParseError(INVALID_VALUE, f"{name} must be a finite number")
    if isinstance(value, str) and _ALPACA_FLOAT.fullmatch(value) is None:
        raise RequestParseError(
            INVALID_VALUE,
            f"{name} must be an ASCII Alpaca number",
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise RequestParseError(
            INVALID_VALUE,
            f"{name} must be a finite number",
        ) from error
    if not math.isfinite(parsed):
        raise RequestParseError(
            INVALID_VALUE,
            f"{name} must be a finite number",
        )
    return parsed
