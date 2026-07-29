from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest

from touptek4040.camera_coordinator import CameraCoordinator, CameraState
from touptek4040.camera_owner import OwnerStartupSpec
from touptek4040.camera_proxy import CameraOwnerProxy
from touptek4040.config import OwnerConfig
from touptek4040.errors import CameraError
from touptek4040.web_console import WebConsole


class BlockingHeaterCamera:
    def __init__(self) -> None:
        self._heater_level = 0
        self.block_readback = False
        self.readback_entered = threading.Event()
        self.readback_release = threading.Event()
        self.readback_release.set()
        self.operations: list[tuple[object, ...]] = []

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def abort(self) -> None:
        pass

    @property
    def heater_supported(self) -> bool:
        self.operations.append(("heater-supported",))
        return True

    @property
    def heater_max_level(self) -> int:
        self.operations.append(("heater-max",))
        return 5

    @property
    def heater_level(self) -> int:
        self.operations.append(("heater-level", self._heater_level))
        self.readback_entered.set()
        if self.block_readback:
            assert self.readback_release.wait(1)
        return self._heater_level

    @heater_level.setter
    def heater_level(self, value: int) -> None:
        self.operations.append(("heater-set", value))
        self._heater_level = value


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


@pytest.mark.parametrize(
    "raw_body",
    [
        b'{"heaterLevel":1,"heaterLevel":2}',
        b'{"heaterLevel":{"x":1,"x":2}}',
        b"[]",
        b"null",
        b"1",
        b'{"heaterLevel":1} true',
        b'{"heaterLevel":{"x":1}}',
    ],
)
def test_web_heater_rejects_ambiguous_or_nonobject_raw_json(
    web_client,
    web_coordinator,
    mutation_headers,
    raw_body,
):
    response = web_client.simulate_post(
        "/web/v1/heater",
        headers=mutation_headers,
        body=raw_body,
    )

    assert response.status_code == 400
    assert response.json == {"error": "Invalid request body"}
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


def test_heater_read_failure_invalidates_web_state_and_disables_writes(
    web_client,
    web_coordinator,
):
    web_coordinator.heater_read_error = CameraError("injected heater read failure")

    response = web_client.simulate_get("/web/v1/state")

    assert response.status_code == 200
    assert response.json["heaterSupported"] is False
    assert response.json["heaterLevel"] is None
    assert response.json["heaterMaxLevel"] is None
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
    web_config,
):
    camera = BlockingHeaterCamera()
    coordinator = CameraCoordinator(lambda _source: camera, "blocking-heater")
    coordinator.set_connected_sync(True)
    console = WebConsole(coordinator, web_config)
    camera.operations.clear()
    camera.block_readback = True
    camera.readback_entered.clear()
    camera.readback_release.clear()
    first_results: list[dict[str, object]] = []
    second_results: list[dict[str, object]] = []
    thread_errors: list[BaseException] = []

    def update(level: int, results: list[dict[str, object]]) -> None:
        try:
            results.append(console.update_heater({"heaterLevel": level}))
        except BaseException as error:
            thread_errors.append(error)

    first = threading.Thread(
        target=update,
        args=(1, first_results),
    )
    second = threading.Thread(
        target=update,
        args=(2, second_results),
    )

    first.start()
    assert camera.readback_entered.wait(1)
    second.start()
    threading.Event().wait(0.05)

    assert second_results == []

    camera.readback_release.set()
    first.join(1)
    second.join(1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert thread_errors == []
    assert first_results == [{"heaterLevel": 1}]
    assert second_results == [{"heaterLevel": 2}]
    assert camera.operations == [
        ("heater-supported",),
        ("heater-max",),
        ("heater-set", 1),
        ("heater-level", 1),
        ("heater-supported",),
        ("heater-max",),
        ("heater-set", 2),
        ("heater-level", 2),
    ]

    coordinator.set_connected_sync(False)


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


def test_heater_ui_keeps_overlapping_requests_ordered_and_refreshes_failures(
    web_client,
):
    web_client.simulate_get("/ui/")
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
      const response = (ok, status, body) => ({{
        ok,
        status,
        statusText: ok ? "OK" : "Error",
        json: async () => body,
      }});
      const posts = [];
      let gets = 0;
      let authoritative = null;
      globalThis.fetch = async (path, options = {{}}) => {{
        if ((options.method ?? "GET") === "POST") {{
          return new Promise((resolve) => posts.push({{
            body: JSON.parse(options.body),
            resolve,
          }}));
        }}
        gets += 1;
        return response(true, 200, authoritative);
      }};
      const errors = [];
      const sync = await import({json.dumps(module)});
      sync.writeHeaterState({{
        connected: true,
        cameraState: "idle",
        heaterSupported: true,
        heaterLevel: 1,
        heaterMaxLevel: 3,
        heaterWritable: true,
      }});
      sync.bindSettings(() => {{}}, (error) => errors.push(error.message));
      const heater = elements["heater-level"];

      heater.value = "2";
      const first = heater.listeners.change();
      heater.value = "3";
      const second = heater.listeners.change();
      sync.writeHeaterState({{
        connected: true,
        cameraState: "idle",
        heaterSupported: true,
        heaterLevel: 1,
        heaterMaxLevel: 3,
        heaterWritable: true,
      }});
      const disabledDuringSse = heater.disabled;

      posts[1].resolve(response(true, 200, {{heaterLevel: 3}}));
      await second;
      const latestCompleted = {{
        value: heater.value,
        disabled: heater.disabled,
      }};
      posts[0].resolve(response(true, 200, {{heaterLevel: 2}}));
      await first;
      const olderCompleted = {{
        value: heater.value,
        disabled: heater.disabled,
      }};

      heater.value = "2";
      const conflict = heater.listeners.change();
      authoritative = {{
        connected: true,
        cameraState: "exposing",
        heaterSupported: true,
        heaterLevel: 3,
        heaterMaxLevel: 3,
        heaterWritable: false,
      }};
      posts[2].resolve(response(
        false,
        409,
        {{error: "camera is exposing"}},
      ));
      await conflict;
      const conflictRefreshed = {{
        value: heater.value,
        disabled: heater.disabled,
        statusMatches: elements["heater-status"].textContent
          === "Level 3 \u00b7 Exposing",
      }};

      heater.value = "0";
      const unsupported = heater.listeners.change();
      authoritative = {{
        connected: true,
        cameraState: "idle",
        heaterSupported: false,
        heaterLevel: null,
        heaterMaxLevel: 0,
        heaterWritable: false,
      }};
      posts[3].resolve(response(
        false,
        500,
        {{error: "camera does not support a heater"}},
      ));
      await unsupported;
      const unsupportedRefreshed = {{
        disabled: heater.disabled,
        status: elements["heater-status"].textContent,
      }};

      process.stdout.write(JSON.stringify({{
        disabledDuringSse,
        latestCompleted,
        olderCompleted,
        conflictRefreshed,
        unsupportedRefreshed,
        posts: posts.map((item) => item.body.heaterLevel),
        gets,
        errors,
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
        "disabledDuringSse": True,
        "latestCompleted": {"value": "3", "disabled": True},
        "olderCompleted": {"value": "3", "disabled": False},
        "conflictRefreshed": {
            "value": "3",
            "disabled": True,
            "statusMatches": True,
        },
        "unsupportedRefreshed": {
            "disabled": True,
            "status": "Not supported",
        },
        "posts": [2, 3, 2, 0],
        "gets": 2,
        "errors": [
            "camera is exposing",
            "camera does not support a heater",
        ],
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
