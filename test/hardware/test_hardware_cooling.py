from __future__ import annotations

import math

import pytest


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

