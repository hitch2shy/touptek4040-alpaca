from __future__ import annotations

import math

import pytest

from touptek4040.camera_coordinator import CameraCoordinator


@pytest.mark.hardware
def test_temperature_and_cooler_are_readable_without_state_toggle(
    real_camera,
    hardware_session,
    hardware_evidence,
) -> None:
    initial = hardware_session.initial
    assert initial is not None

    temperature = real_camera.temperature_c
    target = real_camera.target_temperature_c
    cooler_on = real_camera.cooler_on
    current_voltage, maximum_voltage = real_camera.tec_voltage()
    power = real_camera.cooler_power_percent

    assert math.isfinite(temperature)
    assert -100.0 < temperature < 100.0
    assert target == pytest.approx(initial.tec_target / 10.0)
    assert cooler_on is bool(initial.tec)
    assert 0.0 <= current_voltage <= maximum_voltage
    assert maximum_voltage > 0.0
    assert 0.0 <= power <= 100.0

    # Exercise setters only with their existing values: no thermal or fan transition.
    real_camera.target_temperature_c = target
    real_camera.cooler_on = cooler_on

    handle = real_camera._handle
    assert handle is not None
    assert handle.get_Option(hardware_session.sdk.TOUPCAM_OPTION_FAN) == initial.fan
    hardware_evidence.record(
        "cooling",
        temperature_c=temperature,
        target_c=target,
        cooler_on=cooler_on,
        current_voltage=current_voltage,
        maximum_voltage=maximum_voltage,
        power_percent=power,
        fan=initial.fan,
        toggled=False,
    )


@pytest.mark.hardware
def test_web_heater_level_round_trip_while_main_controller_is_connected(
    real_camera,
    hardware_session,
    hardware_evidence,
) -> None:
    """Use the same coordinator path as Web control and restore in ``finally``."""

    initial = hardware_session.initial
    assert initial is not None
    assert real_camera.heater_supported is True
    assert real_camera.heater_max_level == initial.heater_max_level
    assert real_camera.heater_level == initial.heater_level

    maximum = initial.heater_max_level
    if maximum == 0:
        pytest.skip("device reports no alternate heater level")
    alternate = 0 if initial.heater_level != 0 else 1
    coordinator = CameraCoordinator(lambda _source: real_camera, "hardware")
    coordinator.set_connected_sync(True)
    try:
        confirmed = coordinator.set_heater_level_confirmed_if_idle(alternate)
        assert confirmed.level == alternate
        assert real_camera.heater_level == alternate
        hardware_evidence.record(
            "web-heater",
            main_controller_connected=True,
            initial_level=initial.heater_level,
            maximum_level=maximum,
            requested_level=alternate,
            confirmed_level=confirmed.level,
        )
    finally:
        coordinator.set_heater_level_confirmed_if_idle(initial.heater_level)
        restored = real_camera.heater_level
        hardware_evidence.record(
            "web-heater-restoration",
            initial_level=initial.heater_level,
            final_level=restored,
            restored=restored == initial.heater_level,
        )
        assert restored == initial.heater_level
