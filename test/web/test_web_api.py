from __future__ import annotations


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
    assert response.json["image"] == {"width": 16, "height": 16}
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


def test_cooling_has_an_independent_strict_mutation(
    web_client,
    web_coordinator,
    mutation_headers,
):
    response = web_client.simulate_post(
        "/web/v1/cooling",
        headers=mutation_headers,
        json={"targetTemperature": -22.0, "coolerOn": True},
    )

    assert response.status_code == 200
    assert response.json["targetTemperature"] == -22.0
    assert response.json["coolerOn"] is True
    assert web_coordinator.target_temperature_c == -22.0
    assert web_coordinator.cooler_on is True


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
