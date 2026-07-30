from __future__ import annotations

from ctypes import ArgumentError
from dataclasses import dataclass, field

import pytest

from touptek4040.toupcam_camera import ToupTekCamera, enumerate_cameras


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
    displayname: str = "sMAX16AM-U3-CL-GPS"
    id: str = "usb:1-2"
    model: FakeModel = field(default_factory=FakeModel)


class FakeHandle:
    def __init__(self, pixel_size_error: str | None = None) -> None:
        self.closed = False
        self.pixel_size_error = pixel_size_error

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
        if self.pixel_size_error is not None:
            raise ArgumentError(self.pixel_size_error)
        return (9.0, 9.0)

    def get_ExpTimeRange(self) -> tuple[int, int, int]:
        return (1, 60_000_000, 1)

    def get_ExpoAGainRange(self) -> tuple[int, int, int]:
        return (100, 1600, 100)

    def get_TecTargetRange(self) -> tuple[int, int]:
        return (-200, 200)

    def Close(self) -> None:
        self.closed = True


class FakeToupcam:
    def __init__(
        self,
        pixel_size_error: str | None = None,
        devices: list[FakeDevice] | None = None,
    ) -> None:
        self.handle = FakeHandle(pixel_size_error)
        self.devices = devices if devices is not None else [FakeDevice()]
        self.open_calls: list[str] = []

    def EnumV2(self) -> list[FakeDevice]:
        return self.devices

    def Open(self, device_id: str) -> FakeHandle:
        self.open_calls.append(device_id)
        assert device_id in {
            "usb:1-2",
            "sn:TP241025143218C92B01151AEC40CB7",
        }
        return self.handle


class FakeSdk:
    def __init__(
        self,
        pixel_size_error: str | None = None,
        devices: list[FakeDevice] | None = None,
    ) -> None:
        self.Toupcam = FakeToupcam(pixel_size_error, devices)


def test_identity_uses_serial_not_usb_path() -> None:
    """A changing USB path must not change the public device identifier."""
    fake_sdk = FakeSdk()

    identities = enumerate_cameras(fake_sdk)

    assert identities[0].model == "sMAX16AM-U3-CL-GPS"
    assert identities[0].serial == "TP241025143218C92B01151AEC40CB7"
    assert identities[0].device_id == "TP241025143218C92B01151AEC40CB7"
    assert fake_sdk.Toupcam.handle.closed


def test_camera_discovers_static_capabilities_and_closes_probe() -> None:
    """Discovery supplies real sensor geometry and never retains a probe handle."""
    fake_sdk = FakeSdk()

    camera = ToupTekCamera("TP241025143218C92B01151AEC40CB7", sdk=fake_sdk)

    assert camera.capabilities.width == 4096
    assert camera.capabilities.height == 4096
    assert camera.capabilities.pixel_size_x == 9.0
    assert camera.capabilities.pixel_size_y == 9.0
    assert fake_sdk.Toupcam.handle.closed
    assert fake_sdk.Toupcam.open_calls == [
        "sn:TP241025143218C92B01151AEC40CB7"
    ]


@pytest.mark.parametrize(
    "devices",
    [
        [FakeDevice(), FakeDevice(id="usb:other")],
        [FakeDevice(displayname="WRONG")],
        [FakeDevice(model=FakeModel(name="WRONG"))],
        [FakeDevice(id="")],
    ],
)
def test_production_constructor_rejects_raw_gate_before_any_open(
    devices: list[FakeDevice],
) -> None:
    fake_sdk = FakeSdk(devices=devices)

    with pytest.raises(Exception):
        ToupTekCamera(
            "TP241025143218C92B01151AEC40CB7",
            sdk=fake_sdk,
        )

    assert fake_sdk.Toupcam.open_calls == []


def test_identity_uses_model_pixel_size_when_sdk_wrapper_rejects_ctypes_value() -> None:
    """Python 3.13 can expose a ToupTek wrapper ctypes annotation defect."""
    fake_sdk = FakeSdk("argument 2: TypeError: 'c_ulong' object cannot be interpreted as an integer")

    identity = enumerate_cameras(fake_sdk)[0]

    assert identity.capabilities.pixel_size_x == 9.0
    assert identity.capabilities.pixel_size_y == 9.0


def test_identity_propagates_unrelated_pixel_size_argument_error() -> None:
    """Only the known Python 3.13 c_uint/c_int mismatch is safely recoverable."""
    fake_sdk = FakeSdk("argument 3: invalid device handle")

    with pytest.raises(ArgumentError, match="invalid device handle"):
        enumerate_cameras(fake_sdk)
