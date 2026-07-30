from pathlib import Path

import pytest

from touptek4040.config import (
    MIN_EXPOSURE_OVERHEAD_S,
    OwnerConfig,
    load_config,
)


@pytest.mark.parametrize(
    "field",
    [
        "startup_timeout_s",
        "connect_timeout_s",
        "command_timeout_s",
        "exposure_overhead_s",
        "exposure_multiplier",
        "abort_timeout_s",
        "disconnect_timeout_s",
    ],
)
@pytest.mark.parametrize(
    "value", [True, 0, -1, float("nan"), float("inf"), "2", None]
)
def test_direct_owner_config_rejects_nonfinite_nonpositive_or_nonnumeric_values(
    field, value
):
    values = {
        "startup_timeout_s": 10.0,
        "connect_timeout_s": 20.0,
        "command_timeout_s": 5.0,
        "exposure_overhead_s": 10.0,
        "exposure_multiplier": 1.02,
        "structural_reopen_settle_s": 0.5,
        "abort_timeout_s": 2.0,
        "disconnect_timeout_s": 10.0,
    }
    values[field] = value

    with pytest.raises(ValueError):
        OwnerConfig(**values)


@pytest.mark.parametrize(
    "value", [True, -1, float("nan"), float("inf"), "0.5", None]
)
def test_direct_owner_config_rejects_invalid_structural_reopen_settle(value):
    with pytest.raises(ValueError):
        OwnerConfig(structural_reopen_settle_s=value)


def test_direct_owner_config_accepts_zero_structural_reopen_settle():
    assert OwnerConfig(structural_reopen_settle_s=0).structural_reopen_settle_s == 0.0


@pytest.mark.parametrize("value", [0.0001, 0.25, 0.250999])
def test_owner_config_rejects_overhead_without_fixed_native_margin(value):
    with pytest.raises(ValueError, match="exposure_overhead_s"):
        OwnerConfig(exposure_overhead_s=value)


def test_owner_config_accepts_exact_minimum_exposure_overhead():
    assert (
        OwnerConfig(
            exposure_overhead_s=MIN_EXPOSURE_OVERHEAD_S
        ).exposure_overhead_s
        == MIN_EXPOSURE_OVERHEAD_S
    )


def test_production_defaults_use_real_toupcam():
    cfg = load_config(None, {})

    assert cfg.camera.source == "toupcam"
    assert cfg.camera.readout_mode == "RAW 12-bit"
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 11111
    assert cfg.server.discovery_port == 32227
    assert cfg.web.enabled is True


def test_explicit_simulator_override_is_selected():
    cfg = load_config(None, {"camera.source": "simulator"})

    assert cfg.camera.source == "simulator"


def test_explicit_overrides_win_while_missing_values_stay_from_ini(tmp_path: Path):
    config_path = tmp_path / "camera.ini"
    config_path.write_text(
        "[server]\nport = 12000\n"
        "[camera]\nsource = simulator\nreadout_mode = RAW 8-bit\n"
        "[web]\nenabled = false\n",
        encoding="utf-8",
    )

    cfg = load_config(config_path, {"server.port": 13000})

    assert cfg.config_path == config_path
    assert cfg.server.port == 13000
    assert cfg.camera.source == "simulator"
    assert cfg.camera.readout_mode == "RAW 8-bit"
    assert cfg.web.enabled is False


def test_invalid_readout_mode_in_ini_is_rejected(tmp_path: Path):
    config_path = tmp_path / "camera.ini"
    config_path.write_text("[camera]\nreadout_mode = RAW 10-bit\n", encoding="utf-8")

    with pytest.raises(ValueError, match="readout_mode must be one of"):
        load_config(config_path, {})


def test_invalid_readout_mode_override_is_rejected():
    with pytest.raises(ValueError, match="readout_mode must be one of"):
        load_config(None, {"camera.readout_mode": "RAW 10-bit"})
