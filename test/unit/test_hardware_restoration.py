from __future__ import annotations

from types import SimpleNamespace

from test.hardware.conftest import HardwareSession, HardwareSnapshot


class _Handle:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def Trigger(self, value):
        self.calls.append(("Trigger", value))

    def Stop(self):
        self.calls.append(("Stop",))

    def put_Option(self, option, value):
        self.calls.append(("put_Option", option, value))

    def put_Roi(self, *value):
        self.calls.append(("put_Roi", *value))

    def put_ExpoTime(self, value):
        self.calls.append(("put_ExpoTime", value))

    def put_ExpoAGain(self, value):
        self.calls.append(("put_ExpoAGain", value))

    def put_AutoExpoEnable(self, value):
        self.calls.append(("put_AutoExpoEnable", value))

    def put_HFlip(self, value):
        self.calls.append(("put_HFlip", value))

    def put_VFlip(self, value):
        self.calls.append(("put_VFlip", value))

    def put_Temperature(self, value):
        self.calls.append(("put_Temperature", value))


class _Camera:
    def __init__(self, handle) -> None:
        self._handle = handle
        self.disconnected = False

    def connect(self):
        raise AssertionError("dry-run camera was already connected")

    def disconnect(self):
        self.disconnected = True
        self._handle = None


def test_restore_dry_run_replays_every_mutated_setting_then_closes() -> None:
    handle = _Handle()
    initial = HardwareSnapshot(
        raw=0,
        bitdepth=0,
        binning=1,
        trigger=0,
        tec_target=100,
        tec=1,
        fan=4,
        rotate=0,
        tec_voltage_max=160,
        exposure_us=10_000,
        gain=100,
        roi=(0, 0, 4096, 4096),
        auto_exposure=1,
        hflip=False,
        vflip=False,
        trigger_cancel_mode=None,
    )
    sdk = SimpleNamespace(
        HRESULTException=RuntimeError,
        TOUPCAM_OPTION_TRIGGER=11,
        TOUPCAM_OPTION_RAW=4,
        TOUPCAM_OPTION_BITDEPTH=6,
        TOUPCAM_OPTION_BINNING=23,
        TOUPCAM_OPTION_ROTATE=24,
        TOUPCAM_OPTION_TEC=8,
        TOUPCAM_OPTION_FAN=7,
    )
    session = HardwareSession.__new__(HardwareSession)
    session.sdk = sdk
    session.camera = _Camera(handle)
    session.initial = initial
    session.restoration_errors = []
    session._instrumented_handle = handle
    session._instrumented_handle_ids = {id(handle)}
    session.evidence = SimpleNamespace(record=lambda *args, **kwargs: None)
    session.read_snapshot = lambda: initial

    session.restore_and_close()

    assert handle.calls == [
        ("Trigger", 0),
        ("Stop",),
        ("put_Option", 11, 0),
        ("put_Option", 4, 0),
        ("put_Option", 6, 0),
        ("put_Option", 23, 1),
        ("put_Roi", 0, 0, 4096, 4096),
        ("put_ExpoTime", 10_000),
        ("put_ExpoAGain", 100),
        ("put_AutoExpoEnable", 1),
        ("put_Option", 24, 0),
        ("put_HFlip", False),
        ("put_VFlip", False),
        ("put_Temperature", 100),
        ("put_Option", 8, 1),
        ("put_Option", 7, 4),
    ]
    assert session.camera.disconnected is True
