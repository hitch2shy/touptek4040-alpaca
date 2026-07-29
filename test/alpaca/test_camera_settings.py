from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import falcon.testing
import pytest

from touptek4040.alpaca.responses import (
    INVALID_OPERATION,
    INVALID_VALUE,
    METHOD_NOT_IMPLEMENTED,
    VALUE_NOT_SET,
)
from touptek4040.camera_coordinator import CameraCoordinator, CameraState
from touptek4040.camera_owner import OwnerStartupSpec
from touptek4040.camera_proxy import CameraOwnerProxy
from touptek4040.config import OwnerConfig


def get_value(client, command: str):
    response = client.simulate_get(f"/api/v1/camera/0/{command}")
    assert response.json["ErrorNumber"] == 0
    return response.json["Value"]


def put_value(client, command: str, parameter: str, value: object):
    return client.simulate_put(
        f"/api/v1/camera/0/{command}",
        json={parameter: value},
    )


_LEXICALLY_INVALID_PUTS = [
    pytest.param("binx", {"BinX": "bad"}, "binx", 1, id="bin-x"),
    pytest.param("biny", {"BinY": "bad"}, "biny", 1, id="bin-y"),
    pytest.param("startx", {"StartX": "bad"}, "startx", 0, id="start-x"),
    pytest.param("starty", {"StartY": "bad"}, "starty", 0, id="start-y"),
    pytest.param("numx", {"NumX": "bad"}, "numx", 4096, id="num-x"),
    pytest.param("numy", {"NumY": "bad"}, "numy", 4096, id="num-y"),
    pytest.param("gain", {"Gain": "bad"}, "gain", 100, id="gain"),
    pytest.param(
        "readoutmode",
        {"ReadoutMode": "bad"},
        "readoutmode",
        0,
        id="readout-mode",
    ),
    pytest.param(
        "setccdtemperature",
        {"SetCCDTemperature": "１２.５"},
        "setccdtemperature",
        -10.0,
        id="temperature-fullwidth",
    ),
    pytest.param(
        "setccdtemperature",
        {"SetCCDTemperature": "1_0"},
        "setccdtemperature",
        -10.0,
        id="temperature-underscore",
    ),
    pytest.param(
        "cooleron",
        {"CoolerOn": "yes"},
        "cooleron",
        False,
        id="cooler-on",
    ),
    pytest.param(
        "startexposure",
        {"Duration": "1_0", "Light": "true"},
        None,
        None,
        id="exposure-duration",
    ),
    pytest.param(
        "startexposure",
        {"Duration": "0.1", "Light": "yes"},
        None,
        None,
        id="exposure-light",
    ),
]

_DOMAIN_INVALID_OR_VALID_PUTS = [
    pytest.param("binx", {"BinX": "9"}, id="bin"),
    pytest.param("startx", {"StartX": "4096"}, id="roi-origin"),
    pytest.param("numx", {"NumX": "0"}, id="roi-size"),
    pytest.param("gain", {"Gain": "99"}, id="gain"),
    pytest.param("readoutmode", {"ReadoutMode": "2"}, id="readout-mode"),
    pytest.param(
        "setccdtemperature",
        {"SetCCDTemperature": "-51"},
        id="temperature",
    ),
    pytest.param("cooleron", {"CoolerOn": "true"}, id="cooler-on"),
    pytest.param(
        "startexposure",
        {"Duration": "0", "Light": "true"},
        id="exposure",
    ),
]


def _put_fields(client, command: str, fields: dict[str, object]):
    return client.simulate_put(
        f"/api/v1/camera/0/{command}",
        json=fields,
    )


@pytest.mark.parametrize(
    ("command", "fields", "get_command", "initial_value"),
    _LEXICALLY_INVALID_PUTS,
)
def test_lexical_errors_precede_disconnected_state_without_side_effects(
    command,
    fields,
    get_command,
    initial_value,
    disconnected_client,
    coordinator,
):
    response = _put_fields(disconnected_client, command, fields)
    assert response.status_code == 400
    assert response.json["ErrorNumber"] == INVALID_VALUE
    assert coordinator.snapshot().connected is False
    assert coordinator.frame is None

    connected = _put_fields(
        disconnected_client,
        "connected",
        {"Connected": "true"},
    )
    assert connected.json["ErrorNumber"] == 0
    if get_command is not None:
        assert get_value(disconnected_client, get_command) == initial_value
    assert coordinator.frame is None


@pytest.mark.parametrize(
    ("command", "fields", "get_command", "initial_value"),
    _LEXICALLY_INVALID_PUTS,
)
def test_lexical_errors_precede_busy_state_without_side_effects(
    command,
    fields,
    get_command,
    initial_value,
    client,
    coordinator,
    api_camera,
):
    api_camera.release_acquisition.clear()
    coordinator.start_exposure(0.001, True)
    assert api_camera.acquisition_started.wait(timeout=1)
    try:
        response = _put_fields(client, command, fields)
        assert response.status_code == 400
        assert response.json["ErrorNumber"] == INVALID_VALUE
        assert coordinator.snapshot().camera_state == 2
        assert coordinator.frame is None
        if get_command is not None:
            assert get_value(client, get_command) == initial_value
    finally:
        if coordinator.snapshot().camera_state == 2:
            coordinator.abort_exposure()


@pytest.mark.parametrize(
    ("command", "fields"),
    _DOMAIN_INVALID_OR_VALID_PUTS,
)
def test_valid_lexemes_defer_to_disconnected_state(
    command,
    fields,
    disconnected_client,
):
    response = _put_fields(disconnected_client, command, fields)
    assert response.status_code == 200
    assert response.json["ErrorNumber"] == 0x407


@pytest.mark.parametrize(
    ("command", "fields"),
    _DOMAIN_INVALID_OR_VALID_PUTS,
)
def test_valid_lexemes_defer_to_busy_state(
    command,
    fields,
    client,
    coordinator,
    api_camera,
):
    api_camera.release_acquisition.clear()
    coordinator.start_exposure(0.001, True)
    assert api_camera.acquisition_started.wait(timeout=1)
    try:
        response = _put_fields(client, command, fields)
        assert response.status_code == 200
        assert response.json["ErrorNumber"] == INVALID_OPERATION
        assert coordinator.snapshot().camera_state == 2
    finally:
        if coordinator.snapshot().camera_state == 2:
            coordinator.abort_exposure()


@pytest.mark.parametrize("command", ["binx", "biny"])
@pytest.mark.parametrize("binning", range(1, 9))
def test_writing_either_bin_axis_synchronizes_both(command, binning, client):
    parameter = {"binx": "BinX", "biny": "BinY"}[command]
    response = put_value(client, command, parameter, str(binning))
    assert response.json["ErrorNumber"] == 0
    assert get_value(client, "binx") == binning
    assert get_value(client, "biny") == binning


@pytest.mark.parametrize(
    ("bad_value", "status_code"),
    [
        (0, 200),
        (9, 200),
        (-1, 200),
        (1.5, 400),
        ("1.0", 400),
        (True, 400),
        ("", 400),
        (None, 400),
    ],
)
def test_binning_rejects_invalid_values_without_changing_state(
    bad_value, status_code, client
):
    response = put_value(client, "binx", "BinX", bad_value)
    assert response.status_code == status_code
    assert response.json["ErrorNumber"] == INVALID_VALUE
    assert get_value(client, "binx") == 1
    assert get_value(client, "biny") == 1


@pytest.mark.parametrize(
    ("command", "parameter", "valid", "invalid"),
    [
        ("startx", "StartX", 4095, -1),
        ("starty", "StartY", 4095, -1),
        ("numx", "NumX", 1, 0),
        ("numy", "NumY", 4096, 0),
    ],
)
def test_roi_fields_enforce_individual_ranges_but_defer_combination_validation(
    command, parameter, valid, invalid, client
):
    assert put_value(client, command, parameter, valid).json["ErrorNumber"] == 0
    assert get_value(client, command) == valid
    assert (
        put_value(client, command, parameter, invalid).json["ErrorNumber"]
        == INVALID_VALUE
    )


@pytest.mark.parametrize(
    ("command", "parameter", "value"),
    [
        ("startx", "StartX", 4097),
        ("starty", "StartY", 4097),
        ("numx", "NumX", 4097),
        ("numy", "NumY", 4097),
    ],
)
def test_roi_upper_bounds_are_deferred_until_start_exposure(
    command,
    parameter,
    value,
    client,
):
    assert put_value(client, command, parameter, value).json["ErrorNumber"] == 0
    assert get_value(client, command) == value

    response = client.simulate_put(
        "/api/v1/camera/0/startexposure",
        json={"Duration": "0.1", "Light": "true"},
    )

    assert response.status_code == 200
    assert response.json["ErrorNumber"] == INVALID_VALUE


def test_binning_write_does_not_reject_temporarily_invalid_roi_combination(client):
    assert put_value(client, "startx", "StartX", 4095).json["ErrorNumber"] == 0
    assert put_value(client, "binx", "BinX", 8).json["ErrorNumber"] == 0
    assert get_value(client, "startx") == 4095
    assert get_value(client, "numx") == 4096


def test_gain_is_numeric_and_bounded(client):
    assert get_value(client, "gainmin") == 100
    assert get_value(client, "gainmax") == 1650
    assert put_value(client, "gain", "Gain", "1650").json["ErrorNumber"] == 0
    assert get_value(client, "gain") == 1650
    assert (
        put_value(client, "gain", "Gain", "1651").json["ErrorNumber"]
        == INVALID_VALUE
    )


def test_readout_mode_updates_maxadu_immediately(client):
    assert get_value(client, "readoutmode") == 0
    assert get_value(client, "maxadu") == 4095
    assert (
        put_value(client, "readoutmode", "ReadoutMode", "1").json["ErrorNumber"]
        == 0
    )
    assert get_value(client, "readoutmode") == 1
    assert get_value(client, "maxadu") == 255


@pytest.mark.parametrize(
    ("bad_value", "status_code"),
    [
        (-1, 200),
        (2, 200),
        (1.5, 400),
        ("RAW 8-bit", 400),
        (True, 400),
        (None, 400),
    ],
)
def test_readout_mode_rejects_invalid_enum_values(
    bad_value, status_code, client
):
    response = put_value(client, "readoutmode", "ReadoutMode", bad_value)
    assert response.status_code == status_code
    assert response.json["ErrorNumber"] == INVALID_VALUE


def test_temperature_and_cooler_round_trip_through_coordinator(client, api_camera):
    assert get_value(client, "ccdtemperature") == -4.5
    assert get_value(client, "setccdtemperature") == -10.0
    assert (
        put_value(
            client, "setccdtemperature", "SetCCDTemperature", "-20.5"
        ).json["ErrorNumber"]
        == 0
    )
    assert api_camera.target_temperature_c == -20.5
    assert put_value(client, "cooleron", "CoolerOn", "true").json["ErrorNumber"] == 0
    assert api_camera.cooler_on is True
    assert get_value(client, "coolerpower") == 50.0


@pytest.mark.parametrize(
    ("bad_target", "status_code"),
    [
        (-50.1, 200),
        (40.1, 200),
        ("nan", 400),
        ("inf", 400),
        (True, 400),
        (None, 400),
    ],
)
def test_temperature_target_rejects_invalid_values(
    bad_target, status_code, client
):
    response = put_value(
        client, "setccdtemperature", "SetCCDTemperature", bad_target
    )
    assert response.status_code == status_code
    assert response.json["ErrorNumber"] == INVALID_VALUE


@pytest.mark.parametrize(
    "bad_value",
    [
        "1_0",
        "１２.５",
        "١٢.٥",
        "NaN",
        "Inf",
        "-Infinity",
        "1.2 trailing",
        "0x10",
    ],
)
@pytest.mark.parametrize(
    ("command", "parameter"),
    [
        ("setccdtemperature", "SetCCDTemperature"),
        ("startexposure", "Duration"),
    ],
)
def test_float_parameters_reject_non_ascii_or_non_alpaca_lexemes_without_side_effects(
    command, parameter, bad_value, client, coordinator, api_camera
):
    before_target = api_camera.target_temperature_c
    response = client.simulate_put(
        f"/api/v1/camera/0/{command}",
        json={parameter: bad_value, "Light": "true"},
    )
    assert response.status_code == 400
    assert response.json["ErrorNumber"] == INVALID_VALUE
    assert api_camera.target_temperature_c == before_target
    assert coordinator.snapshot().camera_state == 0
    assert coordinator.frame is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("+1", 1.0),
        ("-1.25", -1.25),
        (".5", 0.5),
        ("1.", 1.0),
        ("1e1", 10.0),
        ("-2.5E+1", -25.0),
    ],
)
def test_temperature_accepts_ascii_alpaca_float_lexemes(
    value, expected, client, api_camera
):
    response = put_value(
        client,
        "setccdtemperature",
        "SetCCDTemperature",
        value,
    )
    assert response.status_code == 200
    assert response.json["ErrorNumber"] == 0
    assert api_camera.target_temperature_c == expected


@pytest.mark.parametrize(
    ("command", "parameter", "new_value", "get_command", "initial_value"),
    [
        ("binx", "BinX", "2", "binx", 1),
        ("startx", "StartX", "2", "startx", 0),
        ("gain", "Gain", "200", "gain", 100),
        ("readoutmode", "ReadoutMode", "1", "readoutmode", 0),
        (
            "setccdtemperature",
            "SetCCDTemperature",
            "-20",
            "setccdtemperature",
            -10.0,
        ),
        ("cooleron", "CoolerOn", "true", "cooleron", False),
    ],
)
def test_setting_commit_is_atomic_with_exposure_start(
    command,
    parameter,
    new_value,
    get_command,
    initial_value,
    client,
    coordinator,
    api_camera,
    monkeypatch,
):
    from touptek4040.alpaca.camera_api import CameraApi

    idle_checked = threading.Event()
    allow_put_to_continue = threading.Event()
    response_holder = {}
    original_require_idle = CameraApi._require_idle

    def pause_after_idle_check(api):
        original_require_idle(api)
        if threading.current_thread().name == "setting-put":
            idle_checked.set()
            assert allow_put_to_continue.wait(timeout=2)

    monkeypatch.setattr(CameraApi, "_require_idle", pause_after_idle_check)
    api_camera.release_acquisition.clear()

    def write_setting():
        response_holder["response"] = put_value(
            client,
            command,
            parameter,
            new_value,
        )

    writer = threading.Thread(target=write_setting, name="setting-put")
    writer.start()
    try:
        assert idle_checked.wait(timeout=1)
        coordinator.start_exposure(0.001, True)
        assert api_camera.acquisition_started.wait(timeout=1)
        allow_put_to_continue.set()
        writer.join(timeout=2)
        assert not writer.is_alive(), "setting PUT deadlocked with exposure start"

        response = response_holder["response"]
        assert response.status_code == 200
        assert response.json["ErrorNumber"] == INVALID_OPERATION
        assert get_value(client, get_command) == initial_value
    finally:
        allow_put_to_continue.set()
        if coordinator.snapshot().camera_state == 2:
            coordinator.abort_exposure()
        writer.join(timeout=2)


def test_snapshot_properties_and_last_exposure_metadata(client, coordinator):
    assert get_value(client, "camerastate") == 0
    assert get_value(client, "imageready") is False
    assert (
        client.simulate_get("/api/v1/camera/0/percentcompleted").json["ErrorNumber"]
        == INVALID_OPERATION
    )
    assert (
        client.simulate_get(
            "/api/v1/camera/0/lastexposureduration"
        ).json["ErrorNumber"]
        == VALUE_NOT_SET
    )

    coordinator.start_exposure(0.001, True)
    thread = coordinator._exposure_thread
    assert thread is not None
    thread.join()
    assert get_value(client, "imageready") is True
    assert get_value(client, "lastexposureduration") == 0.001
    assert (
        get_value(client, "lastexposurestarttime")
        == datetime(2026, 7, 28, 12, 34, 56, tzinfo=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def test_abort_exposure_delegates_to_coordinator(client, coordinator, api_camera):
    api_camera.release_acquisition.clear()
    coordinator.start_exposure(0.001, True)
    assert api_camera.acquisition_started.wait(timeout=1)
    response = client.simulate_put("/api/v1/camera/0/abortexposure")
    assert response.json["ErrorNumber"] == 0
    assert api_camera.abort_calls >= 1


def test_stop_exposure_is_idle_noop_and_canstop_is_false(client):
    assert get_value(client, "canstopexposure") is False
    response = client.simulate_put("/api/v1/camera/0/stopexposure")
    assert response.json["ErrorNumber"] == 0


def test_start_exposure_validates_parameters_without_native_exceptions(client):
    response = client.simulate_put(
        "/api/v1/camera/0/startexposure",
        json={"Duration": "not-a-number", "Light": "true"},
    )
    assert response.status_code == 400
    assert response.json["ErrorNumber"] == INVALID_VALUE


@pytest.mark.parametrize("requested", ["0", "0.0000004", "0.0000116", "0.000012"])
def test_dark_or_bias_clamps_subminimum_duration_to_hardware_minimum(
    client, coordinator, requested
):
    response = client.simulate_put(
        "/api/v1/camera/0/startexposure",
        json={"Duration": requested, "Light": "false"},
    )
    assert response.json["ErrorNumber"] == 0
    thread = coordinator._exposure_thread
    assert thread is not None
    thread.join()
    assert coordinator.frame is not None
    assert coordinator.frame.duration_s == 0.000012
    assert get_value(client, "lastexposureduration") == 0.000012


def test_light_zero_duration_is_invalid(client):
    response = client.simulate_put(
        "/api/v1/camera/0/startexposure",
        json={"Duration": "0", "Light": "true"},
    )

    assert response.json["ErrorNumber"] == INVALID_VALUE


def test_stop_exposure_is_not_implemented_while_exposing(
    client, coordinator, monkeypatch
):
    # The branch itself is covered with a state double because stopping an
    # actual acquisition races the intentionally minimal test camera.
    from touptek4040.camera_coordinator import CameraSnapshot, CameraState

    monkeypatch.setattr(
        coordinator,
        "snapshot",
        lambda: CameraSnapshot(True, False, CameraState.EXPOSING, False, 0, None),
    )
    response = client.simulate_put("/api/v1/camera/0/stopexposure")
    assert response.json["ErrorNumber"] == METHOD_NOT_IMPLEMENTED


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(1, id="waiting"),
        pytest.param(3, id="reading"),
        pytest.param(4, id="download"),
        pytest.param(5, id="error"),
    ],
)
def test_stop_exposure_rejects_nonidle_nonexposing_states(
    client, coordinator, monkeypatch, state
):
    from touptek4040.camera_coordinator import CameraSnapshot, CameraState

    monkeypatch.setattr(
        coordinator,
        "snapshot",
        lambda: CameraSnapshot(
            True,
            False,
            CameraState(state),
            False,
            0,
            None,
        ),
    )

    response = client.simulate_put("/api/v1/camera/0/stopexposure")

    assert response.json["ErrorNumber"] == INVALID_OPERATION


def test_owner_backed_dark_zero_and_idle_stop_preserve_camera_v4_semantics(
    tmp_path: Path,
):
    client, coordinator, state_path = _owner_backed_alpaca_client(tmp_path)
    try:
        started = client.simulate_put(
            "/api/v1/camera/0/startexposure",
            json={"Duration": "0", "Light": "false"},
        )
        assert started.json["ErrorNumber"] == 0
        _wait_until(
            lambda: coordinator.snapshot().camera_state is CameraState.IDLE
            and coordinator.frame is not None
        )

        owner_state = _read_owner_state(state_path)
        acquire_events = [
            event
            for event in owner_state["events"]
            if event["operation"] == "acquire"
        ]
        assert acquire_events[-1]["duration_s"] == 0.000012
        assert get_value(client, "lastexposureduration") == 0.000012

        light_zero = client.simulate_put(
            "/api/v1/camera/0/startexposure",
            json={"Duration": "0", "Light": "true"},
        )
        assert light_zero.json["ErrorNumber"] == INVALID_VALUE
        assert len(
            [
                event
                for event in _read_owner_state(state_path)["events"]
                if event["operation"] == "acquire"
            ]
        ) == 1

        stopped = client.simulate_put("/api/v1/camera/0/stopexposure")
        assert stopped.json["ErrorNumber"] == 0
    finally:
        _close_owner_backed_coordinator(coordinator)


def test_owner_backed_in_progress_stop_reports_method_not_implemented(
    tmp_path: Path,
):
    client, coordinator, state_path = _owner_backed_alpaca_client(
        tmp_path,
        block_acquire=True,
    )
    try:
        started = client.simulate_put(
            "/api/v1/camera/0/startexposure",
            json={"Duration": "1", "Light": "true"},
        )
        assert started.json["ErrorNumber"] == 0
        _wait_until(
            lambda: bool(
                coordinator._camera is not None
                and coordinator._camera.exposing
            )
        )
        time.sleep(0.05)
        assert any(
            event["operation"] == "acquire"
            for event in _read_owner_state(state_path)["events"]
        )
        assert coordinator.snapshot().camera_state is CameraState.EXPOSING

        stopped = client.simulate_put("/api/v1/camera/0/stopexposure")

        assert stopped.json["ErrorNumber"] == METHOD_NOT_IMPLEMENTED
        aborted = client.simulate_put("/api/v1/camera/0/abortexposure")
        assert aborted.json["ErrorNumber"] == 0
        _wait_until(
            lambda: coordinator.snapshot().camera_state is CameraState.IDLE
        )
    finally:
        _close_owner_backed_coordinator(coordinator)


def _owner_backed_alpaca_client(
    tmp_path: Path,
    *,
    block_acquire: bool = False,
):
    from run_alpaca import RuntimeCameraCoordinator
    from touptek4040.alpaca.app import create_app

    state_path = tmp_path / "owner-state.json"
    proxy = CameraOwnerProxy(
        owner_config=OwnerConfig(
            startup_timeout_s=2,
            connect_timeout_s=2,
            command_timeout_s=1,
            exposure_overhead_s=1,
            exposure_multiplier=1,
            abort_timeout_s=1,
            disconnect_timeout_s=2,
        ),
        runtime_directory=tmp_path,
        startup_spec=OwnerStartupSpec(
            serial="TP241025143218C92B01151AEC40CB7",
            backend_module="test.fixtures.fake_owner_backend",
            backend_factory="create_backend",
            backend_arguments={
                "state_path": str(state_path),
                "block_acquire": block_acquire,
            },
        ),
    )
    coordinator = RuntimeCameraCoordinator(
        lambda _source: proxy,
        "owner-backed-test",
        supervisor_close=proxy.close,
    )
    config = SimpleNamespace(
        camera=SimpleNamespace(
            serial="TP241025143218C92B01151AEC40CB7",
            readout_mode="RAW 12-bit",
            gain=100,
        ),
        web=SimpleNamespace(enabled=False),
    )
    coordinator.set_connected_sync(True)
    client = falcon.testing.TestClient(create_app(coordinator, config))
    for command, parameter in (("numx", "NumX"), ("numy", "NumY")):
        response = client.simulate_put(
            f"/api/v1/camera/0/{command}",
            json={parameter: 16},
        )
        assert response.json["ErrorNumber"] == 0
    return (
        client,
        coordinator,
        state_path,
    )


def _read_owner_state(path: Path) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.005)
    raise AssertionError("owner state was not published")


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        time.sleep(0.005)


def _close_owner_backed_coordinator(coordinator: CameraCoordinator) -> None:
    import run_alpaca

    run_alpaca.close_camera_coordinator(coordinator)
