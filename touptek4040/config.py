"""Configuration loading for the ToupTek 4040 camera service."""

from __future__ import annotations

import configparser
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


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
class WebConfig:
    enabled: bool = True


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    directory: Path = Path("logs")


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    camera: CameraConfig
    web: WebConfig
    logging: LoggingConfig
    config_path: Path | None


def runtime_base_dir() -> Path:
    """Return the directory used for runtime files in source and frozen builds."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def load_config(path: Path | None, overrides: Mapping[str, object]) -> AppConfig:
    """Load an optional INI file and apply explicitly supplied overrides."""
    config_path = Path(path) if path is not None else None
    parser = configparser.ConfigParser()
    if config_path is not None:
        parser.read(config_path, encoding="utf-8")

    server = ServerConfig(
        host=_value(parser, overrides, "server", "host", ServerConfig.host),
        port=int(_value(parser, overrides, "server", "port", ServerConfig.port)),
        discovery_port=int(
            _value(parser, overrides, "server", "discovery_port", ServerConfig.discovery_port)
        ),
    )
    camera = CameraConfig(
        source=_value(parser, overrides, "camera", "source", CameraConfig.source),
        serial=_optional(_value(parser, overrides, "camera", "serial", CameraConfig.serial)),
        readout_mode=_value(
            parser, overrides, "camera", "readout_mode", CameraConfig.readout_mode
        ),
        gain=_optional_int(_value(parser, overrides, "camera", "gain", CameraConfig.gain)),
    )
    web = WebConfig(
        enabled=_as_bool(_value(parser, overrides, "web", "enabled", WebConfig.enabled))
    )
    logging = LoggingConfig(
        level=_value(parser, overrides, "logging", "level", LoggingConfig.level),
        directory=_logging_directory(
            _value(parser, overrides, "logging", "directory", LoggingConfig.directory), config_path
        ),
    )
    return AppConfig(server, camera, web, logging, config_path)


def _value(
    parser: configparser.ConfigParser,
    overrides: Mapping[str, object],
    section: str,
    option: str,
    default: object,
) -> object:
    dotted_key = f"{section}.{option}"
    if dotted_key in overrides and overrides[dotted_key] is not None:
        return overrides[dotted_key]
    if parser.has_option(section, option):
        return parser.get(section, option)
    return default


def _optional(value: object) -> str | None:
    return None if value is None or value == "" else str(value)


def _optional_int(value: object) -> int | None:
    return None if value is None or value == "" else int(value)


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
