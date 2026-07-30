from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import RLock
import time
from types import ModuleType
from typing import Any, Iterable

import pytest

from touptek4040.camera_contract import Roi
from touptek4040.toupcam_camera import (
    CameraIdentity,
    ToupTekCamera,
    _identity_from_handle,
)
from touptek4040.toupcam_loader import load_toupcam_module


EXPECTED_MODEL = "sMAX16AM-U3-CL-GPS"
EXPECTED_SERIAL = "TP241025143218C92B01151AEC40CB7"
RESULTS_DIR = Path(__file__).resolve().parents[1] / "results" / "hardware"
_EXPECTED_IDLE_HRESULTS = {0x8000FFFF}
_NATIVE_OPEN_CONFIG_START_ALLOWANCE_S = 2.0
_ABORT_CONTROL_COMPLETION_ALLOWANCE_S = 3.0


def abort_readiness_timeout_s(camera: ToupTekCamera | Any) -> float:
    """Bound reopen, native setup/start, and Abort control completion."""

    return (
        float(camera._structural_reopen_settle_s)
        + _NATIVE_OPEN_CONFIG_START_ALLOWANCE_S
        + _ABORT_CONTROL_COMPLETION_ALLOWANCE_S
    )


def full_frame_roi(binning: int) -> Roi:
    """Largest even output square that maps within the 4096-pixel sensor."""
    if isinstance(binning, bool) or not isinstance(binning, int) or not 1 <= binning <= 8:
        raise ValueError("binning must be an integer from 1 through 8")
    side = ((4096 // binning) // 2) * 2
    return Roi(0, 0, side, side)


def require_unique_raw_target(devices: Iterable[Any]) -> Any:
    """Validate raw EnumV2 metadata before any SDK handle is opened."""

    attached = list(devices)
    if len(attached) != 1:
        raise RuntimeError(
            f"hardware tests require exactly one attached camera; found {len(attached)}"
        )
    device = attached[0]
    raw_id = getattr(device, "id", None)
    model = getattr(getattr(device, "model", None), "name", None)
    display_name = getattr(device, "displayname", None)
    if (
        not isinstance(raw_id, str)
        or not raw_id.strip()
        or model != EXPECTED_MODEL
        or display_name != EXPECTED_MODEL
    ):
        raise RuntimeError(
            "attached device is not the approved target camera "
            f"{EXPECTED_MODEL}/{EXPECTED_SERIAL}"
        )
    return device


def open_verified_target(
    sdk: ModuleType | Any,
    evidence: "HardwareEvidence | None" = None,
) -> CameraIdentity:
    """Gate on EnumV2, then open only the approved serial selector."""

    device = require_unique_raw_target(sdk.Toupcam.EnumV2())
    selector = f"sn:{EXPECTED_SERIAL}"
    if evidence is not None:
        evidence.record(
            "raw-enumeration-gate",
            count=1,
            raw_device_id_present=True,
            model=device.model.name,
            approved_selector=selector,
        )
    handle = sdk.Toupcam.Open(selector)
    if handle is None:
        raise RuntimeError("approved target camera could not be opened")
    try:
        identity = _identity_from_handle(device, handle)
        if (
            identity.serial != EXPECTED_SERIAL
            or identity.model != EXPECTED_MODEL
        ):
            raise RuntimeError(
                "opened device did not verify as the approved target camera"
            )
        return identity
    finally:
        handle.Close()


@dataclass(frozen=True)
class HardwareSnapshot:
    raw: int
    bitdepth: int
    binning: int
    trigger: int
    tec_target: int
    tec: int
    fan: int
    rotate: int
    tec_voltage_max: int
    exposure_us: int
    gain: int
    roi: tuple[int, int, int, int]
    auto_exposure: int
    hflip: bool
    vflip: bool
    trigger_cancel_mode: int | None
    heater_level: int
    heater_max_level: int


class HardwareEvidence:
    """Small append-only JSON evidence; no image buffers are persisted."""

    def __init__(self, results_dir: Path) -> None:
        self.results_dir = results_dir
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self._path = results_dir / "cases.jsonl"
        # SDK handle finalizers can run while an outer evidence payload is
        # being JSON encoded.  Their instrumented Close call records another
        # event on the same thread, so this lock must permit that re-entry.
        self._lock = RLock()
        if os.environ.get("TOUPTEK_HARDWARE_EVIDENCE_APPEND") != "1":
            self._path.write_text("", encoding="utf-8")

    def record(self, case: str, **fields: Any) -> None:
        event = {
            "recorded_utc": datetime.now(timezone.utc).isoformat(),
            "case": case,
            **fields,
        }
        with self._lock:
            with self._path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(event, ensure_ascii=True, sort_keys=True))
                stream.write("\n")


class HardwareSession:
    def __init__(
        self,
        sdk: ModuleType,
        identity: CameraIdentity,
        evidence: HardwareEvidence,
    ) -> None:
        self.sdk = sdk
        self.identity = identity
        self.evidence = evidence
        self.camera = ToupTekCamera(identity.serial, sdk=sdk)
        self.initial: HardwareSnapshot | None = None
        self.restoration_errors: list[str] = []
        self._instrumented_handle: Any | None = None
        self._instrumented_handle_ids: set[int] = set()
        self._sdk_open_wrapped = False
        self.native_calls: list[tuple[str, tuple[Any, ...]]] = []

    def connect_and_snapshot(self) -> None:
        self.camera.connect()
        self.initial = self.read_snapshot()
        self.evidence.record(
            "initial-settings",
            serial=self.identity.serial,
            model=self.identity.model,
            settings=asdict(self.initial),
        )
        handle = self.camera._handle
        assert handle is not None
        handle.put_AutoExpoEnable(False)
        self.instrument_sdk_open()
        self.instrument_handle()
        self.evidence.record(
            "controlled-baseline",
            auto_exposure=False,
            original_auto_exposure=bool(self.initial.auto_exposure),
        )

    def ensure_connected(self) -> None:
        if self.camera._handle is None:
            self.camera.connect()
        self.instrument_handle()

    def instrument_handle(self) -> None:
        handle = self.camera._handle
        self._instrument_specific_handle(handle)

    def instrument_sdk_open(self) -> None:
        if self._sdk_open_wrapped:
            return
        original_open = self.sdk.Toupcam.Open

        def instrumented_open(device_id):
            started_ns = time.monotonic_ns()
            self.evidence.record(
                "native-call",
                operation="Open",
                phase="start",
                monotonic_ns=started_ns,
                device_id=device_id,
            )
            try:
                opened = original_open(device_id)
            except BaseException as error:
                self.evidence.record(
                    "native-call",
                    operation="Open",
                    phase="error",
                    monotonic_ns=time.monotonic_ns(),
                    elapsed_ms=(time.monotonic_ns() - started_ns) / 1_000_000,
                    hresult=(
                        f"0x{(error.hr & 0xFFFFFFFF):08X}"
                        if hasattr(error, "hr")
                        else None
                    ),
                    error_type=type(error).__name__,
                )
                raise
            self._instrument_specific_handle(opened)
            self.evidence.record(
                "native-call",
                operation="Open",
                phase="success",
                monotonic_ns=time.monotonic_ns(),
                elapsed_ms=(time.monotonic_ns() - started_ns) / 1_000_000,
                handle_id=id(opened),
            )
            return opened

        self.sdk.Toupcam.Open = instrumented_open
        self._sdk_open_wrapped = True

    def _instrument_specific_handle(self, handle: Any | None) -> None:
        if handle is None or id(handle) in self._instrumented_handle_ids:
            return
        self._instrumented_handle_ids.add(id(handle))
        self._instrumented_handle = handle
        for name in (
            "put_AutoExpoEnable",
            "put_Option",
            "put_Roi",
            "put_ExpoTime",
            "put_ExpoAGain",
            "put_Temperature",
            "put_HFlip",
            "put_VFlip",
            "StartPullModeWithCallback",
            "TriggerSyncV4",
            "Stop",
            "Close",
        ):
            original = getattr(handle, name)

            def timed(*args, _name=name, _original=original, **kwargs):
                started_ns = time.monotonic_ns()
                requested_wait_ms = (
                    args[0] if _name == "TriggerSyncV4" and args else None
                )
                argument_summary = None
                if _name in {
                    "put_AutoExpoEnable",
                    "put_Option",
                    "put_Roi",
                    "put_ExpoTime",
                    "put_ExpoAGain",
                    "put_Temperature",
                    "put_HFlip",
                    "put_VFlip",
                }:
                    argument_summary = list(args)
                self.native_calls.append((_name, tuple(args)))
                effective_args = args
                diagnostic_default = (
                    _name == "TriggerSyncV4"
                    and os.environ.get("TOUPTEK_DIAGNOSTIC_DEFAULT_TIMEOUT") == "1"
                )
                if diagnostic_default:
                    effective_args = (0, *args[1:])
                self.evidence.record(
                    "native-call",
                    operation=_name,
                    phase="start",
                    monotonic_ns=started_ns,
                    requested_wait_ms=requested_wait_ms,
                    effective_wait_ms=(
                        0 if diagnostic_default else requested_wait_ms
                    ),
                    arguments=argument_summary,
                    handle_id=id(handle),
                    acquiring=self.camera._acquiring,
                    camera_started=self.camera._camera_started,
                )
                try:
                    result = _original(*effective_args, **kwargs)
                except BaseException as error:
                    self.evidence.record(
                        "native-call",
                        operation=_name,
                        phase="error",
                        monotonic_ns=time.monotonic_ns(),
                        elapsed_ms=(time.monotonic_ns() - started_ns) / 1_000_000,
                        hresult=(
                            f"0x{(error.hr & 0xFFFFFFFF):08X}"
                            if hasattr(error, "hr")
                            else None
                        ),
                        error_type=type(error).__name__,
                        handle_id=id(handle),
                        acquiring=self.camera._acquiring,
                        camera_started=self.camera._camera_started,
                    )
                    raise
                if (
                    _name == "Stop"
                    and os.environ.get("TOUPTEK_DIAGNOSTIC_RESET_TRIGGER") == "1"
                ):
                    handle.put_Option(self.sdk.TOUPCAM_OPTION_TRIGGER, 0)
                self.evidence.record(
                    "native-call",
                    operation=_name,
                    phase="success",
                    monotonic_ns=time.monotonic_ns(),
                    elapsed_ms=(time.monotonic_ns() - started_ns) / 1_000_000,
                    acquiring=self.camera._acquiring,
                    camera_started=self.camera._camera_started,
                    handle_id=id(handle),
                )
                return result

            setattr(handle, name, timed)

    def read_snapshot(self) -> HardwareSnapshot:
        handle = self.camera._handle
        if handle is None:
            raise RuntimeError("cannot snapshot a closed camera")
        return HardwareSnapshot(
            raw=handle.get_Option(self.sdk.TOUPCAM_OPTION_RAW),
            bitdepth=handle.get_Option(self.sdk.TOUPCAM_OPTION_BITDEPTH),
            binning=handle.get_Option(self.sdk.TOUPCAM_OPTION_BINNING),
            trigger=handle.get_Option(self.sdk.TOUPCAM_OPTION_TRIGGER),
            tec_target=handle.get_Option(self.sdk.TOUPCAM_OPTION_TECTARGET),
            tec=handle.get_Option(self.sdk.TOUPCAM_OPTION_TEC),
            fan=handle.get_Option(self.sdk.TOUPCAM_OPTION_FAN),
            rotate=handle.get_Option(self.sdk.TOUPCAM_OPTION_ROTATE),
            tec_voltage_max=handle.get_Option(
                self.sdk.TOUPCAM_OPTION_TEC_VOLTAGE_MAX
            ),
            exposure_us=handle.get_ExpoTime(),
            gain=handle.get_ExpoAGain(),
            roi=tuple(handle.get_Roi()),
            auto_exposure=int(handle.get_AutoExpoEnable()),
            hflip=bool(handle.get_HFlip()),
            vflip=bool(handle.get_VFlip()),
            trigger_cancel_mode=self._optional_option(
                "TOUPCAM_OPTION_TRIGGER_CANCEL_MODE"
            ),
            heater_level=handle.get_Option(self.sdk.TOUPCAM_OPTION_HEAT),
            heater_max_level=handle.get_Option(
                self.sdk.TOUPCAM_OPTION_HEAT_MAX
            ),
        )

    def _optional_option(self, name: str) -> int | None:
        handle = self.camera._handle
        if handle is None:
            return None
        try:
            return handle.get_Option(getattr(self.sdk, name))
        except self.sdk.HRESULTException as error:
            if (error.hr & 0xFFFFFFFF) == 0x80004001:
                return None
            raise

    def restore_and_close(self) -> None:
        initial = self.initial
        if initial is None:
            return
        try:
            self.ensure_connected()
            handle = self.camera._handle
            assert handle is not None
            self._quiesce(handle)
            restorations = (
                ("trigger", lambda: handle.put_Option(self.sdk.TOUPCAM_OPTION_TRIGGER, initial.trigger)),
                ("raw", lambda: handle.put_Option(self.sdk.TOUPCAM_OPTION_RAW, initial.raw)),
                ("bitdepth", lambda: handle.put_Option(self.sdk.TOUPCAM_OPTION_BITDEPTH, initial.bitdepth)),
                ("binning", lambda: handle.put_Option(self.sdk.TOUPCAM_OPTION_BINNING, initial.binning)),
                ("roi", lambda: handle.put_Roi(*initial.roi)),
                ("exposure_us", lambda: handle.put_ExpoTime(initial.exposure_us)),
                ("gain", lambda: handle.put_ExpoAGain(initial.gain)),
                ("auto_exposure", lambda: handle.put_AutoExpoEnable(initial.auto_exposure)),
                ("rotate", lambda: handle.put_Option(self.sdk.TOUPCAM_OPTION_ROTATE, initial.rotate)),
                ("hflip", lambda: handle.put_HFlip(initial.hflip)),
                ("vflip", lambda: handle.put_VFlip(initial.vflip)),
                ("tec_target", lambda: handle.put_Temperature(initial.tec_target)),
                ("tec", lambda: handle.put_Option(self.sdk.TOUPCAM_OPTION_TEC, initial.tec)),
                ("fan", lambda: handle.put_Option(self.sdk.TOUPCAM_OPTION_FAN, initial.fan)),
                ("heater_level", lambda: handle.put_Option(self.sdk.TOUPCAM_OPTION_HEAT, initial.heater_level)),
            )
            for name, restore in restorations:
                try:
                    restore()
                except BaseException as error:
                    self.restoration_errors.append(f"{name}: {type(error).__name__}")
            final = self.read_snapshot()
            if final.heater_max_level != initial.heater_max_level:
                self.restoration_errors.append(
                    "heater_max_level changed during the hardware session"
                )
            self.evidence.record(
                "restoration",
                restored=final == initial,
                initial=asdict(initial),
                final=asdict(final),
                errors=self.restoration_errors,
            )
            if final != initial:
                self.restoration_errors.append("final snapshot differs from initial")
        finally:
            try:
                self.camera.disconnect()
            except BaseException as error:
                self.restoration_errors.append(
                    f"disconnect: {type(error).__name__}"
                )
        if self.restoration_errors:
            raise RuntimeError(
                "hardware restoration failed: " + "; ".join(self.restoration_errors)
            )

    def _quiesce(self, handle: Any) -> None:
        for name, operation in (("trigger-cancel", lambda: handle.Trigger(0)), ("stop", handle.Stop)):
            try:
                operation()
            except self.sdk.HRESULTException as error:
                if (error.hr & 0xFFFFFFFF) not in _EXPECTED_IDLE_HRESULTS:
                    self.restoration_errors.append(
                        f"{name}: HRESULT 0x{(error.hr & 0xFFFFFFFF):08X}"
                    )
            except BaseException as error:
                self.restoration_errors.append(f"{name}: {type(error).__name__}")


@pytest.fixture(scope="session")
def hardware_evidence() -> HardwareEvidence:
    if os.environ.get("TOUPTEK_HARDWARE_TEST") != "1":
        pytest.skip("set TOUPTEK_HARDWARE_TEST=1 to access attached hardware")
    return HardwareEvidence(RESULTS_DIR)


@pytest.fixture(scope="session")
def hardware_session(hardware_evidence: HardwareEvidence):
    if os.environ.get("TOUPTEK_HARDWARE_TEST") != "1":
        pytest.skip("set TOUPTEK_HARDWARE_TEST=1 to access attached hardware")
    sdk = load_toupcam_module(Path(__file__).resolve().parents[2])
    identity = open_verified_target(sdk, hardware_evidence)
    session = HardwareSession(sdk, identity, hardware_evidence)
    session.connect_and_snapshot()
    try:
        yield session
    finally:
        session.restore_and_close()


@pytest.fixture(scope="session")
def real_camera(hardware_session: HardwareSession) -> ToupTekCamera:
    return hardware_session.camera
