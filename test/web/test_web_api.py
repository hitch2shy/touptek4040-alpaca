from __future__ import annotations

import threading

from touptek4040.web_console import WebConsole


def test_capabilities_publish_the_real_touptek_sensor_contract(web_client):
    response = web_client.simulate_get("/web/v1/capabilities")

    assert response.status_code == 200
    assert response.json == {
        "sensor": {
            "model": "sMAX16AM-U3-CL-GPS",
            "width": 4096,
            "height": 4096,
            "pixelMicrons": 9.0,
        },
        "readoutModes": ["RAW 12-bit", "RAW 8-bit"],
        "binning": list(range(1, 9)),
        "gain": {"min": 100, "max": 1650},
        "temperature": {"min": -50.0, "max": 40.0},
    }


def test_state_is_read_only_and_reports_generation_and_identity(
    web_client,
    web_coordinator,
):
    response = web_client.simulate_get("/web/v1/state")

    assert response.status_code == 200
    assert response.json["serial"] == "TP241025143218C92B01151AEC40CB7"
    assert response.json["cameraState"] == "idle"
    assert response.json["generation"] == 1
    assert response.json["image"] == {
        "width": 16,
        "height": 16,
        "maxADU": 4095,
        "readoutMode": "RAW 12-bit",
    }
    assert response.json["settings"]["readoutMode"] == "RAW 12-bit"
    assert response.json["preview"] is False
    assert web_coordinator.connect_calls == []
    assert web_coordinator.exposure_calls == []


def test_settings_hydrate_live_cooling_state_without_overwriting_it(
    web_client,
    web_coordinator,
):
    web_coordinator.target_temperature_c = -18.5
    web_coordinator.cooler_on = True

    response = web_client.simulate_get("/web/v1/settings")

    assert response.status_code == 200
    assert response.json["targetTemperature"] == -18.5
    assert response.json["coolerOn"] is True


def test_each_cooling_request_changes_exactly_one_field(
    web_client,
    web_coordinator,
    mutation_headers,
):
    target = web_client.simulate_post(
        "/web/v1/cooling",
        headers=mutation_headers,
        json={"targetTemperature": -22.0},
    )
    cooler = web_client.simulate_post(
        "/web/v1/cooling",
        headers=mutation_headers,
        json={"coolerOn": True},
    )

    assert target.status_code == 200
    assert target.json == {"targetTemperature": -22.0, "coolerOn": False}
    assert cooler.status_code == 200
    assert cooler.json == {"targetTemperature": -22.0, "coolerOn": True}
    assert web_coordinator.target_temperature_c == -22.0
    assert web_coordinator.cooler_on is True


def test_cooling_rejects_a_two_field_request_before_any_setter_runs(
    web_client,
    web_coordinator,
    mutation_headers,
):
    response = web_client.simulate_post(
        "/web/v1/cooling",
        headers=mutation_headers,
        json={"targetTemperature": -22.0, "coolerOn": True},
    )

    assert response.status_code == 400
    assert web_coordinator.target_temperature_c == -10.0
    assert web_coordinator.cooler_on is False


def test_failed_second_cooling_request_cannot_partially_apply_its_field(
    web_client,
    web_coordinator,
    mutation_headers,
):
    original = web_coordinator.set_cooler_on_if_idle

    def fail_cooler(value):
        del value
        raise RuntimeError("injected second-setter failure")

    first = web_client.simulate_post(
        "/web/v1/cooling",
        headers=mutation_headers,
        json={"targetTemperature": -22.0},
    )
    web_coordinator.set_cooler_on_if_idle = fail_cooler
    second = web_client.simulate_post(
        "/web/v1/cooling",
        headers=mutation_headers,
        json={"coolerOn": True},
    )
    web_coordinator.set_cooler_on_if_idle = original

    assert first.status_code == 200
    assert second.status_code == 500
    assert web_coordinator.target_temperature_c == -22.0
    assert web_coordinator.cooler_on is False


def test_concurrent_single_field_cooling_requests_are_coordinator_serialized(
    web_coordinator,
    web_config,
):
    console = WebConsole(web_coordinator, web_config)
    coordinator_gate = threading.Lock()
    target_entered = threading.Event()
    release_target = threading.Event()
    cooler_entered = threading.Event()

    def set_target(value):
        with coordinator_gate:
            target_entered.set()
            assert release_target.wait(1)
            web_coordinator.target_temperature_c = value

    def set_cooler(value):
        with coordinator_gate:
            cooler_entered.set()
            web_coordinator.cooler_on = value

    web_coordinator.set_target_temperature_if_idle = set_target
    web_coordinator.set_cooler_on_if_idle = set_cooler
    first = threading.Thread(
        target=lambda: console.update_cooling({"targetTemperature": -22.0}),
    )
    second = threading.Thread(
        target=lambda: console.update_cooling({"coolerOn": True}),
    )

    first.start()
    assert target_entered.wait(1)
    second.start()
    assert not cooler_entered.wait(0.05)
    release_target.set()
    first.join(1)
    second.join(1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert (web_coordinator.target_temperature_c, web_coordinator.cooler_on) == (
        -22.0,
        True,
    )


def test_settings_commit_and_exposure_snapshot_share_one_linearization_point(
    web_coordinator,
    web_config,
):
    console = WebConsole(web_coordinator, web_config)
    validation_released = threading.Event()
    allow_update_return = threading.Event()
    exposure_complete = threading.Event()
    captured = []

    def paused_update(operation):
        result = operation(web_coordinator.capabilities)
        validation_released.set()
        assert allow_update_return.wait(1)
        return result

    def capture_exposure(duration, light, *, settings):
        del duration, light
        assert validation_released.wait(1)
        captured.append(settings(web_coordinator.capabilities))
        exposure_complete.set()

    web_coordinator.update_settings_if_idle = paused_update
    web_coordinator.start_exposure = capture_exposure
    update = threading.Thread(
        target=lambda: console.update_settings(
            {
                "binning": 2,
                "readoutMode": "RAW 8-bit",
                "gain": 250,
                "roi": {"x": 0, "y": 0, "width": 16, "height": 16},
            }
        ),
    )

    update.start()
    assert validation_released.wait(1)
    exposure = threading.Thread(target=lambda: console.expose(1.5))
    exposure.start()
    assert exposure_complete.wait(1)
    allow_update_return.set()
    update.join(1)
    exposure.join(1)

    assert captured[0].binning == 2
    assert captured[0].readout_mode.name == "RAW8"
    assert captured[0].gain == 250


def test_state_uses_coordinator_preview_truth_after_external_changes(
    web_coordinator,
    web_config,
):
    console = WebConsole(web_coordinator, web_config)
    console.preview(True)
    web_coordinator.preview_active = False

    assert console.state()["preview"] is False

    web_coordinator.preview_active = True
    assert console.state()["preview"] is True


def test_valid_exposure_uses_the_coordinator_with_an_immutable_settings_snapshot(
    web_client,
    web_coordinator,
    mutation_headers,
):
    settings = {
        "binning": 2,
        "readoutMode": "RAW 8-bit",
        "gain": 250,
        "roi": {"x": 0, "y": 0, "width": 16, "height": 16},
    }
    update = web_client.simulate_post(
        "/web/v1/settings",
        headers=mutation_headers,
        json=settings,
    )
    exposure = web_client.simulate_post(
        "/web/v1/exposure",
        headers=mutation_headers,
        json={"duration": 1.5},
    )

    assert update.status_code == 200
    assert exposure.status_code == 202
    duration, light, snapshot = web_coordinator.exposure_calls[-1]
    assert duration == 1.5
    assert light is True
    assert snapshot.binning == 2
    assert snapshot.readout_mode.name == "RAW8"
    assert snapshot.gain == 250
    assert snapshot.roi.width == 16


def test_preview_and_abort_call_only_the_shared_coordinator(
    web_client,
    web_coordinator,
    mutation_headers,
):
    started = web_client.simulate_post(
        "/web/v1/preview",
        headers=mutation_headers,
        json={"enabled": True},
    )
    aborted = web_client.simulate_post(
        "/web/v1/abort",
        headers=mutation_headers,
        json={},
    )

    assert started.status_code == 202
    assert aborted.status_code == 202
    assert web_coordinator.preview_calls == ["start"]
    assert web_coordinator.abort_calls == 1
