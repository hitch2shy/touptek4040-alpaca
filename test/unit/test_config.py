from pathlib import Path

import pytest

from touptek4040.config import load_config


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
