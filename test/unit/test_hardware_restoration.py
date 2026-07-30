from __future__ import annotations

from types import SimpleNamespace
from threading import Event, Thread

from test.hardware.conftest import (
    HardwareSession,
    HardwareSnapshot,
    abort_readiness_timeout_s,
)
from test.hardware.conftest import HardwareEvidence


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
        heater_level=2,
        heater_max_level=4,
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
        TOUPCAM_OPTION_HEAT=45,
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
        ("put_Option", 45, 2),
    ]
    assert session.camera.disconnected is True


def test_hardware_evidence_allows_same_thread_finalizer_style_reentry(
    tmp_path,
) -> None:
    evidence = HardwareEvidence(tmp_path)
    completed = Event()

    def record_nested() -> None:
        with evidence._lock:
            evidence.record("nested-close")
        completed.set()

    worker = Thread(target=record_nested, daemon=True)
    worker.start()
    assert completed.wait(1.0), "evidence lock deadlocked on same-thread re-entry"
    worker.join(1.0)


def test_hardware_evidence_can_append_across_bounded_stage_processes(
    tmp_path,
    monkeypatch,
) -> None:
    HardwareEvidence(tmp_path).record("first-stage")
    monkeypatch.setenv("TOUPTEK_HARDWARE_EVIDENCE_APPEND", "1")

    HardwareEvidence(tmp_path).record("matrix-stage")

    text = (tmp_path / "cases.jsonl").read_text(encoding="utf-8")
    assert '"case": "first-stage"' in text
    assert '"case": "matrix-stage"' in text


def test_hardware_runner_creates_a_zero_byte_jsonl_without_utf8_bom() -> None:
    script = (
        __import__("pathlib").Path(__file__).parents[1]
        / "scripts"
        / "run_hardware.ps1"
    ).read_text(encoding="utf-8")

    assert '(Join-Path $results "cases.jsonl")' in script
    assert 'Remove-Item -LiteralPath $generatedArtifacts' in script
    assert "New-Item -ItemType File -Path $cases" in script
    assert "Set-Content -LiteralPath $cases" not in script


def test_hardware_runner_propagates_summary_failure() -> None:
    script = (
        __import__("pathlib").Path(__file__).parents[1]
        / "scripts"
        / "run_hardware.ps1"
    ).read_text(encoding="utf-8")

    assert "$summaryExitCode = $LASTEXITCODE" in script
    assert "if ($summaryExitCode -ne 0)" in script
    assert "Hardware evidence summary failed" in script


def test_abort_readiness_timeout_includes_configured_reopen_and_native_budget() -> None:
    camera = SimpleNamespace(
        _structural_reopen_settle_s=0.5,
    )

    assert abort_readiness_timeout_s(camera) == 5.5
