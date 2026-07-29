"""Controllable ToupCam test double for hardware-adapter tests."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
from threading import Event
from typing import Any


E_INVALIDARG = 0x80070057
E_NOTIMPL = 0x80004001
E_UNEXPECTED = 0x8000FFFF
E_WRONG_THREAD = 0x8001010E
E_GEN_FAILURE = 0x8007001F
E_BUSY = 0x800700AA
E_PENDING = 0x8000000A
E_TIMEOUT = 0x8001011F
E_FAIL = 0x80004005

OPTION_RAW = 0x04
OPTION_BITDEPTH = 0x06
OPTION_FAN = 0x07
OPTION_TEC = 0x08
OPTION_TRIGGER = 0x0B
OPTION_TECTARGET = 0x0F
OPTION_BINNING = 0x17
OPTION_TEC_VOLTAGE = 0x20
OPTION_TEC_VOLTAGE_MAX = 0x21
OPTION_HEAT = 0x2D
OPTION_HEAT_MAX = 0x2E
FLAG_HEAT = 0x40000000


class FakeHResultError(Exception):
    def __init__(self, hr: int) -> None:
        super().__init__(f"HRESULT 0x{hr & 0xFFFFFFFF:08x}")
        self.hr = hr


@dataclass
class FakeResolution:
    width: int
    height: int


@dataclass
class FakeModel:
    name: str = "sMAX16AM-U3-CL-GPS"
    flag: int = 0
    xpixsz: float = 9.0
    ypixsz: float = 9.0
    res: tuple[FakeResolution, ...] = field(
        default_factory=lambda: (FakeResolution(4096, 4096),)
    )


@dataclass
class FakeDevice:
    displayname: str = "Camera at USB path 1-2"
    id: str = "usb:1-2"
    model: FakeModel = field(default_factory=FakeModel)


class FakeFrameInfoV3:
    def __init__(self) -> None:
        self.width = 0
        self.height = 0
        self.flag = 0
        self.seq = 0
        self.timestamp = 0
        self.shutterseq = 0
        self.expotime = 0
        self.expogain = 0
        self.blacklevel = 0


class FakeFrameInfoV4:
    def __init__(self) -> None:
        self.v3 = FakeFrameInfoV3()


class FakeToupCam:
    """One reopenable camera handle with controllable synchronous triggering."""

    def __init__(self, sdk: "FakeSdk") -> None:
        self.sdk = sdk
        self.closed = False
        self.options = {
            OPTION_RAW: 0,
            OPTION_BITDEPTH: 0,
            OPTION_BINNING: 1,
            OPTION_TRIGGER: 0,
            OPTION_FAN: 2,
            OPTION_TEC: 1,
            OPTION_TECTARGET: -100,
            OPTION_TEC_VOLTAGE: 50,
            OPTION_TEC_VOLTAGE_MAX: 60,
            OPTION_HEAT: 2,
            OPTION_HEAT_MAX: 4,
        }
        self.roi = (0, 0, 0, 0)
        self.exposure_us = 10_000
        self.auto_exposure = True
        self.gain = 100
        self.temperature_deci_c = -75
        self.trigger_started = Event()
        self.trigger_release = Event()
        self.trigger_cancelled = Event()
        self.start_entered = Event()
        self.start_release = Event()
        self.stop_entered = Event()
        self.stop_release = Event()
        self.block_start = False
        self.block_stop = False
        self.block_temperature = False
        self.block_option = False
        self.block_put_option = False
        self.block_trigger = False
        self.deliver_after_abort = False
        self.trigger_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.stop_error: Exception | None = None
        self.temperature_entered = Event()
        self.temperature_release = Event()
        self.option_entered = Event()
        self.option_release = Event()
        self.put_option_entered = Event()
        self.put_option_release = Event()
        self.close_error: Exception | None = None
        self.frame_size_override: tuple[int, int] | None = None
        self.frame_info_size_override: tuple[int, int] | None = None
        self.started = False

    def SerialNumber(self) -> str:
        return "TP241025143218C92B01151AEC40CB7"

    def FwVersion(self) -> str:
        return "1.2.3"

    def HwVersion(self) -> str:
        return "4.5.6"

    def ProductionDate(self) -> str:
        return "20241025"

    def FpgaVersion(self) -> str:
        return "7.8.9"

    def get_Resolution(self, index: int) -> tuple[int, int]:
        assert index == 0
        return (4096, 4096)

    def get_PixelSize(self, index: int) -> tuple[float, float]:
        assert index == 0
        return (9.0, 9.0)

    def get_ExpTimeRange(self) -> tuple[int, int, int]:
        return (1, 60_000_000, 1)

    def get_ExpoAGainRange(self) -> tuple[int, int, int]:
        return (100, 1650, 100)

    def get_TecTargetRange(self) -> tuple[int, int]:
        return (-200, 200)

    def Close(self) -> None:
        self.sdk.calls.append(("Close",))
        if self.close_error is not None:
            raise self.close_error
        self.closed = True

    def put_Option(self, option: int, value: int) -> None:
        self.sdk.calls.append(("put_Option", option, value))
        self.put_option_entered.set()
        if self.block_put_option:
            self.put_option_release.wait(2)
        self.options[option] = value

    def get_Option(self, option: int) -> int:
        self.sdk.calls.append(("get_Option", option))
        self.option_entered.set()
        if self.block_option:
            self.option_release.wait(2)
        return self.options[option]

    def put_Roi(self, x: int, y: int, width: int, height: int) -> None:
        self.sdk.calls.append(("put_Roi", x, y, width, height))
        self.roi = (x, y, width, height)

    def get_Roi(self) -> tuple[int, int, int, int]:
        return self.roi

    def put_ExpoTime(self, exposure_us: int) -> None:
        self.sdk.calls.append(("put_ExpoTime", exposure_us))
        self.exposure_us = exposure_us

    def get_ExpoTime(self) -> int:
        return self.exposure_us

    def put_AutoExpoEnable(self, enabled: bool) -> None:
        self.sdk.calls.append(("put_AutoExpoEnable", enabled))
        self.auto_exposure = bool(enabled)

    def get_AutoExpoEnable(self) -> bool:
        return self.auto_exposure

    def put_ExpoAGain(self, gain: int) -> None:
        self.sdk.calls.append(("put_ExpoAGain", gain))
        self.gain = gain

    def get_ExpoAGain(self) -> int:
        self.sdk.calls.append(("get_ExpoAGain",))
        return self.gain

    def get_FinalSize(self) -> tuple[int, int]:
        self.sdk.calls.append(("get_FinalSize",))
        if self.frame_size_override is not None:
            return self.frame_size_override
        _, _, roi_width, roi_height = self.roi
        width = roi_width or 4096
        height = roi_height or 4096
        binning = self.options[OPTION_BINNING]
        return ((width // binning) & ~1, (height // binning) & ~1)

    def TriggerSyncV4(
        self,
        timeout_ms: int,
        image_data: Any,
        bits: int,
        row_pitch: int,
        info: FakeFrameInfoV4,
    ) -> None:
        self.sdk.calls.append(("TriggerSyncV4", timeout_ms, bits, row_pitch))
        if not self.started:
            raise FakeHResultError(E_UNEXPECTED)
        if getattr(image_data, "_type_", None) is not ctypes.c_char:
            raise ctypes.ArgumentError(
                "argument 3 must match the SDK wrapper's ctypes.c_char_p"
            )
        self.trigger_started.set()
        if self.block_trigger:
            self.trigger_release.wait(2)
        if self.trigger_cancelled.is_set() and not self.deliver_after_abort:
            raise FakeHResultError(E_UNEXPECTED)
        if self.trigger_error is not None:
            raise self.trigger_error
        width, height = self.get_FinalSize()
        raw12 = bool(self.options[OPTION_BITDEPTH])
        maximum = 4095 if raw12 else 255
        element_type = ctypes.c_ushort if raw12 else ctypes.c_ubyte
        output = ctypes.cast(image_data, ctypes.POINTER(element_type))
        for index in range(width * height):
            output[index] = index % (maximum + 1)
        info_width, info_height = self.frame_info_size_override or (width, height)
        info.v3.width = info_width
        info.v3.height = info_height
        info.v3.expotime = self.exposure_us

    def StartPullModeWithCallback(self, callback: Any, context: Any) -> None:
        self.sdk.calls.append(("StartPullModeWithCallback", callback, context))
        self.start_entered.set()
        if self.block_start:
            self.start_release.wait(2)
        self.trigger_cancelled.clear()
        self.trigger_started.clear()
        self.trigger_release.clear()
        self.started = True

    def Stop(self) -> None:
        self.sdk.calls.append(("Stop",))
        if self.block_stop and not self.stop_entered.is_set():
            self.stop_entered.set()
            self.stop_release.wait(2)
        if self.stop_error is not None:
            raise self.stop_error
        self.trigger_cancelled.set()
        self.trigger_release.set()
        self.started = False

    def Trigger(self, count: int) -> None:
        self.sdk.calls.append(("Trigger", count))
        if count == 0:
            if self.cancel_error is not None:
                raise self.cancel_error
            self.trigger_cancelled.set()

    def get_Temperature(self) -> int:
        self.sdk.calls.append(("get_Temperature",))
        self.temperature_entered.set()
        if self.block_temperature:
            self.temperature_release.wait(2)
        return self.temperature_deci_c

    def put_Temperature(self, value: int) -> None:
        self.sdk.calls.append(("put_Temperature", value))
        self.options[OPTION_TECTARGET] = value


class FakeToupcamApi:
    def __init__(self, sdk: "FakeSdk") -> None:
        self.sdk = sdk
        self.handle = FakeToupCam(sdk)

    def EnumV2(self) -> list[FakeDevice]:
        return [FakeDevice()]

    def Open(self, device_id: str) -> FakeToupCam:
        assert device_id in {"usb:1-2", "sn:TP241025143218C92B01151AEC40CB7"}
        self.sdk.calls.append(("Open", device_id))
        self.handle.closed = False
        return self.handle


class FakeSdk:
    TOUPCAM_OPTION_RAW = OPTION_RAW
    TOUPCAM_OPTION_BITDEPTH = OPTION_BITDEPTH
    TOUPCAM_OPTION_FAN = OPTION_FAN
    TOUPCAM_OPTION_TEC = OPTION_TEC
    TOUPCAM_OPTION_TRIGGER = OPTION_TRIGGER
    TOUPCAM_OPTION_TECTARGET = OPTION_TECTARGET
    TOUPCAM_OPTION_BINNING = OPTION_BINNING
    TOUPCAM_OPTION_TEC_VOLTAGE = OPTION_TEC_VOLTAGE
    TOUPCAM_OPTION_TEC_VOLTAGE_MAX = OPTION_TEC_VOLTAGE_MAX
    TOUPCAM_OPTION_HEAT = OPTION_HEAT
    TOUPCAM_OPTION_HEAT_MAX = OPTION_HEAT_MAX
    TOUPCAM_FLAG_HEAT = FLAG_HEAT
    ToupcamFrameInfoV4 = FakeFrameInfoV4
    HRESULTException = FakeHResultError

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.Toupcam = FakeToupcamApi(self)
