"""Configuration loading for the ToupTek 4040 camera service."""

from __future__ import annotations

import configparser
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .owner_protocol import MAX_EXPOSURE_DURATION_S


NATIVE_OWNER_TIMEOUT_MARGIN_S = 0.25
NATIVE_TIMEOUT_CEILING_ALLOWANCE_S = 0.001
NATIVE_TIMEOUT_FLOAT_ALLOWANCE_S = 0.001
MIN_EXPOSURE_OVERHEAD_S = (
    NATIVE_OWNER_TIMEOUT_MARGIN_S
    + NATIVE_TIMEOUT_CEILING_ALLOWANCE_S
    + NATIVE_TIMEOUT_FLOAT_ALLOWANCE_S
)
TOUPCAM_MAX_TRIGGER_TIMEOUT_MS = (1 << 32) - 1


def native_trigger_timeout_ms(
    duration_s: float,
    multiplier: float,
    owner_overhead_s: float,
) -> int:
    """Return the exact unsigned-millisecond SDK timeout derivation."""

    native_overhead_s = owner_overhead_s - MIN_EXPOSURE_OVERHEAD_S
    timeout_s = duration_s * multiplier + native_overhead_s
    scaled = timeout_s * 1000.0
    if not math.isfinite(scaled) or scaled <= 0:
        raise ValueError("native trigger timeout must be finite and positive")
    return math.ceil(scaled)


def _validate_exposure_deadline_pair(
    multiplier: float,
    owner_overhead_s: float,
) -> None:
    """Prove the maximum supported exposure fits the SDK and fixed margin."""

    try:
        native_ms = native_trigger_timeout_ms(
            MAX_EXPOSURE_DURATION_S,
            multiplier,
            owner_overhead_s,
        )
        owner_timeout_s = (
            MAX_EXPOSURE_DURATION_S * multiplier + owner_overhead_s
        )
    except (OverflowError, ValueError) as error:
        raise ValueError(
            "owner.exposure_multiplier cannot preserve the native timeout margin"
        ) from error
    if (
        not math.isfinite(owner_timeout_s)
        or native_ms > TOUPCAM_MAX_TRIGGER_TIMEOUT_MS
    ):
        raise ValueError(
            "owner.exposure_multiplier exceeds the Toupcam unsigned "
            "millisecond timeout range"
        )
    native_timeout_s = native_ms / 1000.0
    representation_allowance_s = 4 * max(
        math.ulp(owner_timeout_s),
        math.ulp(native_timeout_s),
    )
    if (
        owner_timeout_s - native_timeout_s
        < NATIVE_OWNER_TIMEOUT_MARGIN_S + representation_allowance_s
    ):
        raise ValueError(
            "owner exposure timeout arithmetic cannot preserve the fixed "
            "native timeout margin"
        )


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 11111
    discovery_port: int = 32227


@dataclass(frozen=True)
class CameraConfig:
    source: str = "toupcam"
    serial: str | None = None
    readout_mode: str = "RAW 12-bit"
    gain: int | None = None


@dataclass(frozen=True)
class OwnerConfig:
    startup_timeout_s: float = 10.0
    connect_timeout_s: float = 20.0
    command_timeout_s: float = 5.0
    exposure_overhead_s: float = 10.0
    exposure_multiplier: float = 1.02
    structural_reopen_settle_s: float = 0.5
    abort_timeout_s: float = 2.0
    disconnect_timeout_s: float = 10.0

    def __post_init__(self) -> None:
        for field_name in (
            "startup_timeout_s",
            "connect_timeout_s",
            "command_timeout_s",
            "exposure_overhead_s",
            "exposure_multiplier",
            "abort_timeout_s",
            "disconnect_timeout_s",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
            ):
                raise ValueError(
                    f"owner.{field_name} must be a positive finite number"
                )
            try:
                normalized = float(value)
            except (OverflowError, ValueError) as error:
                raise ValueError(
                    f"owner.{field_name} must be a positive finite number"
                ) from error
            if not math.isfinite(normalized) or normalized <= 0:
                raise ValueError(
                    f"owner.{field_name} must be a positive finite number"
                )
            object.__setattr__(self, field_name, normalized)
        value = self.structural_reopen_settle_s
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(
                "owner.structural_reopen_settle_s must be a non-negative finite number"
            )
        object.__setattr__(self, "structural_reopen_settle_s", float(value))
        if self.exposure_overhead_s < MIN_EXPOSURE_OVERHEAD_S:
            raise ValueError(
                "owner.exposure_overhead_s must be at least "
                f"{MIN_EXPOSURE_OVERHEAD_S:g} seconds to keep the native "
                "camera timeout below the owner deadline"
            )
        _validate_exposure_deadline_pair(
            self.exposure_multiplier,
            self.exposure_overhead_s,
        )


@dataclass(frozen=True)
class WebConfig:
    enabled: bool = True


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    directory: Path = Path("logs")


@dataclass(frozen=True)
class SecurityConfig:
    trusted_lan_only: bool = True


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    camera: CameraConfig
    web: WebConfig
    logging: LoggingConfig
    config_path: Path | None
    owner: OwnerConfig = OwnerConfig()
    security: SecurityConfig = SecurityConfig()


_JSON_SECTIONS = {
    "server": {"host", "port", "discovery_port"},
    "camera": {"source", "serial", "readout_mode", "gain"},
    "owner": {
        "startup_timeout_s",
        "connect_timeout_s",
        "command_timeout_s",
        "exposure_overhead_s",
        "exposure_multiplier",
        "structural_reopen_settle_s",
        "abort_timeout_s",
        "disconnect_timeout_s",
    },
    "web": {"enabled"},
    "logging": {"level", "directory"},
    "security": {"trusted_lan_only"},
}


def runtime_base_dir() -> Path:
    """Return the directory used for runtime files in source and frozen builds."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object, rejecting duplicate member names."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def reject_nonfinite(value: str) -> object:
    """Reject JSON extensions such as NaN and Infinity."""
    raise ValueError(f"non-finite JSON value {value!r}")


def load_json_object_strict(path: Path) -> dict[str, object]:
    """Read one JSON object without duplicate keys or non-finite constants."""
    try:
        loaded = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_nonfinite,
        )
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON configuration {path}") from error
    if not isinstance(loaded, dict):
        raise ValueError("JSON configuration root must be an object")
    return loaded


def load_config(path: Path | None, overrides: Mapping[str, object]) -> AppConfig:
    """Load strict JSON or explicitly selected legacy INI configuration."""
    config_path = Path(path) if path is not None else None
    parser: configparser.ConfigParser | None = None
    document: dict[str, object] | None = None
    if config_path is not None:
        if config_path.suffix == ".json":
            document = load_json_object_strict(config_path)
            _validate_json_config(document)
        elif config_path.suffix == ".ini":
            parser = configparser.ConfigParser()
            parser.read(config_path, encoding="utf-8")
        else:
            raise ValueError("configuration file suffix must be .json or .ini")

    def value(section: str, option: str, default: object) -> object:
        dotted_key = f"{section}.{option}"
        if dotted_key in overrides and overrides[dotted_key] is not None:
            return overrides[dotted_key]
        if document is not None:
            return _json_value(document, section, option, default)
        if parser is not None and parser.has_option(section, option):
            return parser.get(section, option)
        return default

    server = ServerConfig(
        host=str(value("server", "host", ServerConfig.host)),
        port=_as_int(value("server", "port", ServerConfig.port), "server.port"),
        discovery_port=_as_int(
            value("server", "discovery_port", ServerConfig.discovery_port),
            "server.discovery_port",
        ),
    )
    camera = CameraConfig(
        source=str(value("camera", "source", CameraConfig.source)),
        serial=_optional(value("camera", "serial", CameraConfig.serial)),
        readout_mode=_readout_mode(
            value("camera", "readout_mode", CameraConfig.readout_mode)
        ),
        gain=_optional_int(value("camera", "gain", CameraConfig.gain)),
    )
    owner = OwnerConfig(
        startup_timeout_s=_positive_float(
            value("owner", "startup_timeout_s", OwnerConfig.startup_timeout_s),
            "owner.startup_timeout_s",
        ),
        connect_timeout_s=_positive_float(
            value("owner", "connect_timeout_s", OwnerConfig.connect_timeout_s),
            "owner.connect_timeout_s",
        ),
        command_timeout_s=_positive_float(
            value("owner", "command_timeout_s", OwnerConfig.command_timeout_s),
            "owner.command_timeout_s",
        ),
        exposure_overhead_s=_positive_float(
            value("owner", "exposure_overhead_s", OwnerConfig.exposure_overhead_s),
            "owner.exposure_overhead_s",
        ),
        exposure_multiplier=_positive_float(
            value("owner", "exposure_multiplier", OwnerConfig.exposure_multiplier),
            "owner.exposure_multiplier",
        ),
        structural_reopen_settle_s=_nonnegative_float(
            value(
                "owner",
                "structural_reopen_settle_s",
                OwnerConfig.structural_reopen_settle_s,
            ),
            "owner.structural_reopen_settle_s",
        ),
        abort_timeout_s=_positive_float(
            value("owner", "abort_timeout_s", OwnerConfig.abort_timeout_s),
            "owner.abort_timeout_s",
        ),
        disconnect_timeout_s=_positive_float(
            value("owner", "disconnect_timeout_s", OwnerConfig.disconnect_timeout_s),
            "owner.disconnect_timeout_s",
        ),
    )
    web = WebConfig(enabled=_as_bool(value("web", "enabled", WebConfig.enabled)))
    logging = LoggingConfig(
        level=str(value("logging", "level", LoggingConfig.level)),
        directory=_logging_directory(
            value("logging", "directory", LoggingConfig.directory), config_path
        ),
    )
    security = SecurityConfig(
        trusted_lan_only=_as_bool(
            value("security", "trusted_lan_only", SecurityConfig.trusted_lan_only)
        )
    )
    return AppConfig(server, camera, web, logging, config_path, owner, security)


def _validate_json_config(document: dict[str, object]) -> None:
    unknown_sections = set(document) - set(_JSON_SECTIONS)
    missing_sections = set(_JSON_SECTIONS) - set(document)
    if unknown_sections or missing_sections:
        raise ValueError("JSON configuration must contain exactly the supported sections")
    for section, allowed_keys in _JSON_SECTIONS.items():
        values = document[section]
        if not isinstance(values, dict):
            raise ValueError(f"JSON section {section!r} must be an object")
        unknown_keys = set(values) - allowed_keys
        if unknown_keys:
            raise ValueError(f"JSON section {section!r} contains unsupported keys")
        _validate_json_section_values(section, values)


def _validate_json_section_values(section: str, values: dict[object, object]) -> None:
    string_keys = {
        "server": {"host"},
        "camera": {"source", "readout_mode"},
        "logging": {"level", "directory"},
    }.get(section, set())
    for key in string_keys:
        if key in values and not isinstance(values[key], str):
            raise ValueError(f"JSON value {section}.{key} must be a string")
    if section == "server":
        for key in {"port", "discovery_port"}:
            if key in values and (not isinstance(values[key], int) or isinstance(values[key], bool)):
                raise ValueError(f"JSON value {section}.{key} must be an integer")
    if section == "camera":
        if "serial" in values and values["serial"] is not None and not isinstance(values["serial"], str):
            raise ValueError("JSON value camera.serial must be a string or null")
        if "gain" in values and values["gain"] is not None and (
            not isinstance(values["gain"], int) or isinstance(values["gain"], bool)
        ):
            raise ValueError("JSON value camera.gain must be an integer or null")
    if section == "owner":
        for key, value in values.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise ValueError(f"JSON value owner.{key} must be a finite number")
            if key == "structural_reopen_settle_s":
                if value < 0:
                    raise ValueError(
                        "JSON value owner.structural_reopen_settle_s "
                        "must be non-negative"
                    )
            elif value <= 0:
                raise ValueError(f"JSON value owner.{key} must be positive")
    if section in {"web", "security"}:
        key = "enabled" if section == "web" else "trusted_lan_only"
        if key in values and not isinstance(values[key], bool):
            raise ValueError(f"JSON value {section}.{key} must be a boolean")


def _json_value(
    document: Mapping[str, object], section: str, option: str, default: object
) -> object:
    values = document[section]
    assert isinstance(values, dict)
    return values.get(option, default)


def _optional(value: object) -> str | None:
    return None if value is None or value == "" else str(value)


def _optional_int(value: object) -> int | None:
    return None if value is None or value == "" else _as_int(value, "camera.gain")


def _as_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error


def _positive_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a positive finite number") from error
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return result


def _nonnegative_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a non-negative finite number") from error
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a non-negative finite number")
    return result


def _readout_mode(value: object) -> str:
    mode = str(value)
    allowed_modes = {"RAW 12-bit", "RAW 8-bit"}
    if mode not in allowed_modes:
        raise ValueError(f"readout_mode must be one of {sorted(allowed_modes)}; got {mode!r}")
    return mode


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _logging_directory(value: object, config_path: Path | None) -> Path:
    directory = Path(value)
    if directory.is_absolute():
        return directory
    base_dir = config_path.parent if config_path is not None else runtime_base_dir()
    return (base_dir / directory).resolve()
