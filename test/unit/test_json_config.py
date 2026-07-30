from __future__ import annotations

import json
from pathlib import Path

import pytest

from touptek4040.config import (
    OwnerConfig,
    SecurityConfig,
    load_config,
    load_json_object_strict,
)


EXPECTED_SERIAL = "TP241025143218C92B01151AEC40CB7"


def _json_config(**sections: object) -> dict[str, object]:
    config: dict[str, object] = {
        "server": {"host": "127.0.0.1", "port": 11111, "discovery_port": 32227},
        "camera": {
            "source": "toupcam",
            "serial": EXPECTED_SERIAL,
            "readout_mode": "RAW 12-bit",
            "gain": None,
        },
        "owner": {
            "startup_timeout_s": 10.0,
            "connect_timeout_s": 20.0,
            "command_timeout_s": 5.0,
            "exposure_overhead_s": 10.0,
            "exposure_multiplier": 1.02,
            "structural_reopen_settle_s": 0.5,
            "abort_timeout_s": 2.0,
            "disconnect_timeout_s": 10.0,
        },
        "web": {"enabled": True},
        "logging": {"level": "INFO", "directory": "logs"},
        "security": {"trusted_lan_only": True},
    }
    config.update(sections)
    return config


def _write_json_config(path: Path, **sections: object) -> None:
    path.write_text(json.dumps(_json_config(**sections)), encoding="utf-8")


def test_json_is_primary_and_owner_timeouts_are_loaded(tmp_path: Path):
    path = tmp_path / "touptek4040.json"
    _write_json_config(path)

    config = load_config(path, {})

    assert config.owner.connect_timeout_s == 20.0
    assert config.owner.structural_reopen_settle_s == 0.5
    assert config.camera.serial == EXPECTED_SERIAL
    assert config.logging.directory == (tmp_path / "logs").resolve()


def test_owner_and_security_defaults_are_available_without_a_config_file():
    config = load_config(None, {})

    assert config.owner == OwnerConfig()
    assert config.security == SecurityConfig()


@pytest.mark.parametrize(
    "contents",
    [
        '{"server": {}, "server": {}}',
        '{"value": NaN}',
        '{"value": Infinity}',
        '{"value": -Infinity}',
    ],
)
def test_strict_json_rejects_duplicate_or_nonfinite_values(tmp_path: Path, contents: str):
    path = tmp_path / "invalid.json"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError):
        load_json_object_strict(path)


def test_strict_json_requires_an_object_at_the_document_root(tmp_path: Path):
    path = tmp_path / "invalid.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError):
        load_json_object_strict(path)


@pytest.mark.parametrize("missing_section", ["server", "camera", "owner", "web", "logging", "security"])
def test_json_config_requires_each_object_section(tmp_path: Path, missing_section: str):
    path = tmp_path / "invalid.json"
    document = _json_config()
    document.pop(missing_section)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(path, {})


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("server", "unknown", "value"),
        ("camera", "unknown", "value"),
        ("owner", "unknown", "value"),
        ("web", "unknown", "value"),
        ("logging", "unknown", "value"),
        ("security", "unknown", "value"),
    ],
)
def test_json_config_rejects_unknown_keys(tmp_path: Path, section: str, key: str, value: object):
    path = tmp_path / "invalid.json"
    document = _json_config()
    document[section][key] = value  # type: ignore[index]
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(path, {})


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("server", "port", True),
        ("camera", "gain", True),
        ("owner", "connect_timeout_s", True),
        ("web", "enabled", "true"),
        ("logging", "directory", 42),
        ("security", "trusted_lan_only", 1),
    ],
)
def test_json_config_rejects_wrong_field_types(tmp_path: Path, section: str, key: str, value: object):
    path = tmp_path / "invalid.json"
    document = _json_config()
    document[section][key] = value  # type: ignore[index]
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(path, {})


@pytest.mark.parametrize("timeout_key", [
    "startup_timeout_s",
    "connect_timeout_s",
    "command_timeout_s",
    "exposure_overhead_s",
    "exposure_multiplier",
    "abort_timeout_s",
    "disconnect_timeout_s",
])
@pytest.mark.parametrize("invalid_value", [0, -1.0])
def test_json_config_rejects_nonpositive_owner_timeouts(
    tmp_path: Path, timeout_key: str, invalid_value: float
):
    path = tmp_path / "invalid.json"
    document = _json_config()
    document["owner"][timeout_key] = invalid_value  # type: ignore[index]
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(path, {})


@pytest.mark.parametrize("invalid_value", [-1.0, True, "0.5", None])
def test_json_config_rejects_invalid_structural_reopen_settle(
    tmp_path: Path, invalid_value: object
):
    path = tmp_path / "invalid.json"
    document = _json_config()
    document["owner"]["structural_reopen_settle_s"] = invalid_value  # type: ignore[index]
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(path, {})


def test_json_paths_are_resolved_relative_to_the_config_file(tmp_path: Path):
    path = tmp_path / "settings" / "camera.json"
    path.parent.mkdir()
    _write_json_config(path, logging={"level": "WARNING", "directory": "runtime-logs"})

    config = load_config(path, {})

    assert config.logging.directory == (path.parent / "runtime-logs").resolve()


def test_dotted_overrides_take_precedence_over_json_values(tmp_path: Path):
    path = tmp_path / "camera.json"
    _write_json_config(path)

    config = load_config(
        path,
        {"server.port": 13000, "owner.connect_timeout_s": 30.0, "security.trusted_lan_only": False},
    )

    assert config.server.port == 13000
    assert config.owner.connect_timeout_s == 30.0
    assert config.security.trusted_lan_only is False


def test_explicit_ini_suffix_preserves_legacy_compatibility(tmp_path: Path):
    path = tmp_path / "camera.ini"
    path.write_text("[server]\nport = 12000\n[camera]\nsource = simulator\n", encoding="utf-8")

    config = load_config(path, {})

    assert config.server.port == 12000
    assert config.camera.source == "simulator"


@pytest.mark.parametrize("suffix", [".toml", ".yaml", ".cfg"])
def test_only_json_and_explicit_legacy_ini_suffixes_are_accepted(tmp_path: Path, suffix: str):
    path = tmp_path / f"camera{suffix}"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(path, {})
