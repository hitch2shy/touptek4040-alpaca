"""Read-only ToupTek camera discovery keyed by immutable serial numbers."""

from __future__ import annotations

import sys
from ctypes import ArgumentError
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from .camera_contract import CameraCapabilities
from .errors import CameraInvalidValueError, CameraNotConnectedError
from .toupcam_loader import load_default_toupcam_module


@dataclass(frozen=True)
class CameraIdentity:
    """A hardware identity and its immutable, read-only characteristics."""

    device_id: str
    display_name: str
    model: str
    serial: str
    capabilities: CameraCapabilities
    firmware: str
    hardware: str
    production_date: str
    fpga: str
    model_flags: int
    exposure_time_range_us: tuple[int, int, int]
    gain_range: tuple[int, int, int]
    tec_target_range_deci_c: tuple[int, int] | None


def enumerate_cameras(sdk: ModuleType | Any | None = None) -> list[CameraIdentity]:
    """Open every enumerated camera briefly to read its serial and capabilities."""
    active_sdk = sdk if sdk is not None else load_default_toupcam_module()
    identities: list[CameraIdentity] = []
    for device in active_sdk.Toupcam.EnumV2():
        handle = active_sdk.Toupcam.Open(device.id)
        if handle is None:
            continue
        try:
            identities.append(_identity_from_handle(device, handle))
        finally:
            handle.Close()
    return identities


class ToupTekCamera:
    """Stable ToupTek camera selection with cached read-only capabilities."""

    def __init__(self, serial: str | None, sdk: ModuleType | Any | None = None) -> None:
        self._sdk = sdk if sdk is not None else load_default_toupcam_module()
        identities = enumerate_cameras(self._sdk)
        if serial is None:
            if len(identities) != 1:
                raise CameraInvalidValueError("serial is required unless exactly one camera is attached")
            self.identity = identities[0]
        else:
            self.identity = next((item for item in identities if item.serial == serial), None)
            if self.identity is None:
                raise CameraNotConnectedError(f"camera with serial {serial!r} is not attached")
        self.serial = self.identity.serial
        self._handle: Any | None = None

    @property
    def capabilities(self) -> CameraCapabilities:
        return self.identity.capabilities

    def connect(self) -> None:
        if self._handle is None:
            self._handle = self._sdk.Toupcam.Open(f"sn:{self.serial}")
            if self._handle is None:
                raise CameraNotConnectedError(f"could not open camera with serial {self.serial!r}")

    def disconnect(self) -> None:
        if self._handle is not None:
            self._handle.Close()
            self._handle = None

    def abort(self) -> None:
        if self._handle is None:
            raise CameraNotConnectedError("camera is not connected")
        self._handle.Stop()


def _identity_from_handle(device: Any, handle: Any) -> CameraIdentity:
    model = device.model
    resolution = handle.get_Resolution(0)
    try:
        pixel_size = handle.get_PixelSize(0)
    except ArgumentError as error:
        # SDK 20250623 annotates this call as c_int but passes c_uint internally.
        # Python 3.13 rejects that mismatch; EnumV2 exposes the same static values.
        if not _is_known_pixel_size_wrapper_error(error):
            raise
        pixel_size = (model.xpixsz, model.ypixsz)
    capabilities = CameraCapabilities(
        width=resolution[0],
        height=resolution[1],
        pixel_size_x=pixel_size[0],
        pixel_size_y=pixel_size[1],
        readout_modes=("RAW 12-bit", "RAW 8-bit"),
        max_bin=8,
        can_abort=True,
        can_stop=False,
    )
    tec_target_range = handle.get_TecTargetRange() if hasattr(handle, "get_TecTargetRange") else None
    serial = handle.SerialNumber()
    return CameraIdentity(
        device_id=serial,
        display_name=device.displayname,
        model=model.name,
        serial=serial,
        capabilities=capabilities,
        firmware=handle.FwVersion(),
        hardware=handle.HwVersion(),
        production_date=handle.ProductionDate(),
        fpga=handle.FpgaVersion(),
        model_flags=model.flag,
        exposure_time_range_us=handle.get_ExpTimeRange(),
        gain_range=handle.get_ExpoAGainRange(),
        tec_target_range_deci_c=tec_target_range,
    )


def _is_known_pixel_size_wrapper_error(error: ArgumentError) -> bool:
    """Match only the SDK 20250623 Python 3.13 second-argument annotation defect."""
    message = str(error)
    return (
        sys.version_info[:2] == (3, 13)
        and "argument 2:" in message
        and ("c_ulong" in message or "c_uint" in message)
        and "cannot be interpreted as an integer" in message
    )
