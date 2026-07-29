from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest

from touptek4040.camera_coordinator import CameraState
from touptek4040.camera_owner import OwnerStartupSpec
from touptek4040.camera_proxy import CameraOwnerProxy
from touptek4040.config import OwnerConfig
from touptek4040.errors import CameraError
from touptek4040.web_console import WebConsole


def test_supported_idle_state_reports_device_heater_contract(
    web_client,
):
    state = web_client.simulate_get("/web/v1/state").json

    assert state["heaterSupported"] is True
    assert state["heaterLevel"] == 0
    assert state["heaterMaxLevel"] == 5
    assert state["heaterWritable"] is True


@pytest.mark.parametrize("level", range(6))
def test_web_heater_accepts_off_and_every_device_reported_level(
    web_client,
    web_coordinator,
    mutation_headers,
    level,
):
    response = web_client.simulate_post(
        "/web/v1/heater",
        headers=mutation_headers,
        json={"heaterLevel": level},
    )

    assert response.status_code == 200
    assert response.json == {"heaterLevel": level}
    assert web_coordinator.heater_writes == [level]
    assert web_coordinator.heater_level_reads == 1


@pytest.mark.parametrize("value", [True, 1.0, "1", -1, 6])
def test_web_heater_rejects_non_exact_or_out_of_range_values_before_write(
    web_client,
    web_coordinator,
    mutation_headers,
    value,
):
    response = web_client.simulate_post(
        "/web/v1/heater",
        headers=mutation_headers,
        json={"heaterLevel": value},
    )

    assert response.status_code == 400
    assert set(response.json) == {"error"}
    assert web_coordinator.heater_writes == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"heaterlevel": 1},
        {"HeaterLevel": 1},
        {"heaterLevel": 1, "extra": 2},
    ],
)
def test_web_heater_accepts_only_the_exact_single_json_member(
    web_client,
    web_coordinator,
    mutation_headers,
    payload,
):
    response = web_client.simulate_post(
        "/web/v1/heater",
        headers=mutation_headers,
        json=payload,
    )

    assert response.status_code == 400
    assert set(response.json) == {"error"}
    assert web_coordinator.heater_writes == []


def test_unsupported_and_disconnected_heater_states_are_not_writable(
    web_client,
    web_coordinator,
    mutation_headers,
):
    web_coordinator.heater_supported = False
    unsupported = web_client.simulate_get("/web/v1/state").json
    unsupported_write = web_client.simulate_post(
        "/web/v1/heater",
        headers=mutation_headers,
        json={"heaterLevel": 0},
    )

    assert unsupported["heaterSupported"] is False
    assert unsupported["heaterLevel"] is None
    assert unsupported["heaterMaxLevel"] == 0
    assert unsupported["heaterWritable"] is False
    assert unsupported_write.status_code == 500
    assert web_coordinator.heater_writes == []

    web_coordinator.connected = False
    disconnected = web_client.simulate_get("/web/v1/state").json
    disconnected_write = web_client.simulate_post(
        "/web/v1/heater",
        headers=mutation_headers,
        json={"heaterLevel": 0},
    )

    assert disconnected["heaterSupported"] is False
    assert disconnected["heaterLevel"] is None
    assert disconnected["heaterMaxLevel"] is None
    assert disconnected["heaterWritable"] is False
    assert disconnected_write.status_code == 503
    assert web_coordinator.heater_writes == []


def test_heater_read_failure_keeps_state_available_but_disables_writes(
    web_client,
    web_coordinator,
):
    web_coordinator.heater_read_error = CameraError("injected heater read failure")

    response = web_client.simulate_get("/web/v1/state")

    assert response.status_code == 200
    assert response.json["heaterSupported"] is True
    assert response.json["heaterLevel"] is None
    assert response.json["heaterMaxLevel"] == 5
    assert response.json["heaterWritable"] is False


def test_heater_write_failure_does_not_publish_an_unconfirmed_level(
    web_client,
    web_coordinator,
    mutation_headers,
):
    web_coordinator.heater_write_error = CameraError(
        "injected heater write failure"
    )

    response = web_client.simulate_post(
        "/web/v1/heater",
        headers=mutation_headers,
        json={"heaterLevel": 3},
    )
    state = web_client.simulate_get("/web/v1/state").json

    assert response.status_code == 500
    assert state["heaterLevel"] == 0
    assert web_coordinator.heater_writes == []


def test_exposure_keeps_last_confirmed_level_visible_and_rejects_write(
    web_client,
    web_coordinator,
    mutation_headers,
):
    web_coordinator._heater_level = 3
    confirmed = web_client.simulate_get("/web/v1/state").json
    reads_before_exposure = (
        web_coordinator.heater_capability_reads,
        web_coordinator.heater_level_reads,
    )
    web_coordinator.camera_state = CameraState.EXPOSING
    web_coordinator.heater_read_error = AssertionError(
        "exposure state must use the last confirmed heater level"
    )

    exposing = web_client.simulate_get("/web/v1/state").json
    write = web_client.simulate_post(
        "/web/v1/heater",
        headers=mutation_headers,
        json={"heaterLevel": 4},
    )

    assert confirmed["heaterLevel"] == 3
    assert exposing["heaterLevel"] == 3
    assert exposing["heaterWritable"] is False
    assert (
        web_coordinator.heater_capability_reads,
        web_coordinator.heater_level_reads,
    ) == reads_before_exposure
    assert write.status_code == 409
    assert web_coordinator.heater_writes == []


def test_two_web_heater_writes_flow_through_one_coordinator_serialization_point(
    web_coordinator,
    web_config,
):
    console = WebConsole(web_coordinator, web_config)
    coordinator_gate = threading.Lock()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    writes: list[int] = []

    def serialized_write(level: int) -> None:
        with coordinator_gate:
            if level == 1:
                first_entered.set()
                assert release_first.wait(1)
            else:
                second_entered.set()
            web_coordinator._heater_level = level
            writes.append(level)

    web_coordinator.set_heater_level_if_idle = serialized_write
    first = threading.Thread(
        target=lambda: console.update_heater({"heaterLevel": 1}),
    )
    second = threading.Thread(
        target=lambda: console.update_heater({"heaterLevel": 2}),
    )

    first.start()
    assert first_entered.wait(1)
    second.start()
    assert not second_entered.wait(0.05)
    release_first.set()
    first.join(1)
    second.join(1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert writes == [1, 2]


def test_owner_backed_web_heater_operates_while_main_controller_stays_connected(
    tmp_path: Path,
):
    console, coordinator, state_path = _owner_backed_console(tmp_path)
    try:
        initial = console.state()
        result = console.update_heater({"heaterLevel": 3})
        owner_state = _read_json(state_path)

        assert initial["heaterSupported"] is True
        assert initial["heaterLevel"] == 2
        assert initial["heaterMaxLevel"] == 4
        assert result == {"heaterLevel": 3}
        assert coordinator.snapshot().connected is True
        assert owner_state["backend_pid"] != os.getpid()
        assert owner_state["heater_level"] == 3
        assert owner_state["counts"]["set_heater_level"] == 1
    finally:
        _close_owner_backed_coordinator(coordinator)


def test_heater_ui_generates_device_levels_disables_and_rolls_back(
    web_client,
):
    html = web_client.simulate_get("/ui/").text
    assert 'id="heater-level"' in html
    assert 'id="heater-status"' in html

    module = (
        Path(__file__).parents[2]
        / "touptek4040"
        / "web"
        / "settings-sync.js"
    ).as_uri()
    script = f"""
      class Element {{
        constructor(id = "") {{
          this.id = id;
          this.value = "";
          this.textContent = "";
          this.checked = false;
          this.disabled = false;
          this.listeners = {{}};
          this.options = [];
          this.classList = {{add() {{}}, remove() {{}}}};
        }}
        addEventListener(type, callback) {{ this.listeners[type] = callback; }}
        replaceChildren(...children) {{ this.options = children; }}
      }}
      const ids = [
        "gain", "gain-value", "binning", "readout-mode", "roi-x", "roi-y",
        "roi-width", "roi-height", "apply-settings", "settings-message",
        "target-temperature", "cooler-on", "heater-level", "heater-status",
      ];
      const elements = Object.fromEntries(ids.map((id) => [id, new Element(id)]));
      globalThis.document = {{
        getElementById: (id) => elements[id],
        createElement: () => new Element(),
      }};
      let response = {{ok: true, status: 200, statusText: "OK",
        json: async () => ({{heaterLevel: 2}})}};
      globalThis.fetch = async () => response;
      const sync = await import({json.dumps(module)});
      sync.writeHeaterState({{
        connected: true,
        cameraState: "idle",
        heaterSupported: true,
        heaterLevel: 2,
        heaterMaxLevel: 3,
        heaterWritable: true,
      }});
      const idle = {{
        labels: elements["heater-level"].options.map((item) => item.textContent),
        value: elements["heater-level"].value,
        disabled: elements["heater-level"].disabled,
      }};
      sync.bindSettings(() => {{}}, () => {{}});
      sync.writeHeaterState({{
        connected: true,
        cameraState: "exposing",
        heaterSupported: true,
        heaterLevel: 2,
        heaterMaxLevel: 3,
        heaterWritable: false,
      }});
      const exposing = {{
        value: elements["heater-level"].value,
        disabled: elements["heater-level"].disabled,
      }};
      response = {{ok: false, status: 500, statusText: "Error",
        json: async () => ({{error: "heater write failed"}})}};
      elements["heater-level"].disabled = false;
      elements["heater-level"].value = "3";
      await elements["heater-level"].listeners.change();
      process.stdout.write(JSON.stringify({{
        idle,
        exposing,
        rolledBack: elements["heater-level"].value,
      }}));
    """

    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "idle": {
            "labels": ["Off", "1", "2", "3"],
            "value": "2",
            "disabled": False,
        },
        "exposing": {"value": "2", "disabled": True},
        "rolledBack": "2",
    }


def _owner_backed_console(tmp_path: Path):
    from run_alpaca import RuntimeCameraCoordinator

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
            backend_arguments={"state_path": str(state_path)},
        ),
    )
    coordinator = RuntimeCameraCoordinator(
        lambda _source: proxy,
        "owner-backed-web-test",
        supervisor_close=proxy.close,
    )
    coordinator.set_connected_sync(True)
    config = SimpleNamespace(
        camera=SimpleNamespace(
            serial="TP241025143218C92B01151AEC40CB7",
            readout_mode="RAW 12-bit",
            gain=100,
        ),
        web=SimpleNamespace(enabled=True),
    )
    return WebConsole(coordinator, config), coordinator, state_path


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _close_owner_backed_coordinator(coordinator) -> None:
    import run_alpaca

    run_alpaca.close_camera_coordinator(coordinator)
