"""ASCOM Alpaca common device members for the Camera device."""

from __future__ import annotations

from typing import Any

from ..camera_coordinator import CameraCoordinator
from .request import get_parameter
from .responses import (
    ACTION_NOT_IMPLEMENTED,
    METHOD_NOT_IMPLEMENTED,
    AlpacaError,
)

DEVICE_NAME = "ToupTek 4040 Alpaca Camera"
DEVICE_DESCRIPTION = "ToupTek sMAX16AM-U3-CL-GPS 4096x4096 mono camera"
DRIVER_VERSION = "0.1.0"
DRIVER_INFO = (
    "ToupTek4040 0.1.0; SDK 20250623; firmware 4.6.7.20240903; "
    "hardware 1.0; FPGA 3.17"
)


class CommonApi:
    """Implement common Alpaca members independently of Falcon routing."""

    def __init__(
        self,
        coordinator: CameraCoordinator,
        camera_api: Any,
    ) -> None:
        self._coordinator = coordinator
        self._camera_api = camera_api

    def get(self, command: str, request: Any) -> object:
        del request
        snapshot = self._coordinator.snapshot()
        if command == "connected":
            return snapshot.connected
        if command == "connecting":
            return snapshot.connecting
        if command == "description":
            return DEVICE_DESCRIPTION
        if command == "devicestate":
            return self._camera_api.device_state()
        if command == "driverinfo":
            return DRIVER_INFO
        if command == "driverversion":
            return DRIVER_VERSION
        if command == "interfaceversion":
            return 4
        if command == "name":
            return DEVICE_NAME
        if command == "supportedactions":
            return []
        raise AlpacaError(
            METHOD_NOT_IMPLEMENTED,
            f"Common property {command} is not implemented",
        )

    def put(self, command: str, request: Any) -> None:
        if command == "action":
            raise AlpacaError(
                ACTION_NOT_IMPLEMENTED,
                "No driver-specific actions are supported",
            )
        if command in {"commandblind", "commandbool", "commandstring"}:
            raise AlpacaError(
                METHOD_NOT_IMPLEMENTED,
                f"{command} is not implemented",
            )
        if command == "connect":
            self._coordinator.connect_async()
            return
        if command == "disconnect":
            self._coordinator.disconnect_async()
            return
        if command == "connected":
            connected = _alpaca_bool(
                get_parameter(request, "Connected"),
                "Connected",
            )
            self._coordinator.set_connected_sync(connected)
            return
        raise AlpacaError(
            METHOD_NOT_IMPLEMENTED,
            f"Common method {command} is not implemented",
        )


def _alpaca_bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        folded = value.casefold()
        if folded == "true":
            return True
        if folded == "false":
            return False
    from .responses import INVALID_VALUE

    raise AlpacaError(INVALID_VALUE, f"{name} must be true or false")
