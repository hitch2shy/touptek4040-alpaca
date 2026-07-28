from __future__ import annotations

from touptek4040.alpaca.manifest import CAMERA_V4_ENDPOINTS, EndpointSpec


EXPECTED_CAMERA_V4_ENDPOINTS = (
    ("PUT", "action", "common"),
    ("PUT", "commandblind", "common"),
    ("PUT", "commandbool", "common"),
    ("PUT", "commandstring", "common"),
    ("PUT", "connect", "common"),
    ("GET", "connected", "common"),
    ("PUT", "connected", "common"),
    ("GET", "connecting", "common"),
    ("GET", "description", "common"),
    ("GET", "devicestate", "common"),
    ("PUT", "disconnect", "common"),
    ("GET", "driverinfo", "common"),
    ("GET", "driverversion", "common"),
    ("GET", "interfaceversion", "common"),
    ("GET", "name", "common"),
    ("GET", "supportedactions", "common"),
    ("GET", "bayeroffsetx", "camera"),
    ("GET", "bayeroffsety", "camera"),
    ("GET", "binx", "camera"),
    ("PUT", "binx", "camera"),
    ("GET", "biny", "camera"),
    ("PUT", "biny", "camera"),
    ("GET", "camerastate", "camera"),
    ("GET", "cameraxsize", "camera"),
    ("GET", "cameraysize", "camera"),
    ("GET", "canabortexposure", "camera"),
    ("GET", "canasymmetricbin", "camera"),
    ("GET", "canfastreadout", "camera"),
    ("GET", "cangetcoolerpower", "camera"),
    ("GET", "canpulseguide", "camera"),
    ("GET", "cansetccdtemperature", "camera"),
    ("GET", "canstopexposure", "camera"),
    ("GET", "ccdtemperature", "camera"),
    ("GET", "cooleron", "camera"),
    ("PUT", "cooleron", "camera"),
    ("GET", "coolerpower", "camera"),
    ("GET", "electronsperadu", "camera"),
    ("GET", "exposuremax", "camera"),
    ("GET", "exposuremin", "camera"),
    ("GET", "exposureresolution", "camera"),
    ("GET", "fastreadout", "camera"),
    ("PUT", "fastreadout", "camera"),
    ("GET", "fullwellcapacity", "camera"),
    ("GET", "gain", "camera"),
    ("PUT", "gain", "camera"),
    ("GET", "gainmax", "camera"),
    ("GET", "gainmin", "camera"),
    ("GET", "gains", "camera"),
    ("GET", "hasshutter", "camera"),
    ("GET", "heatsinktemperature", "camera"),
    ("GET", "imagearray", "image"),
    ("GET", "imagearrayvariant", "image"),
    ("GET", "imageready", "camera"),
    ("GET", "ispulseguiding", "camera"),
    ("GET", "lastexposureduration", "camera"),
    ("GET", "lastexposurestarttime", "camera"),
    ("GET", "maxadu", "camera"),
    ("GET", "maxbinx", "camera"),
    ("GET", "maxbiny", "camera"),
    ("GET", "numx", "camera"),
    ("PUT", "numx", "camera"),
    ("GET", "numy", "camera"),
    ("PUT", "numy", "camera"),
    ("GET", "offset", "camera"),
    ("PUT", "offset", "camera"),
    ("GET", "offsetmax", "camera"),
    ("GET", "offsetmin", "camera"),
    ("GET", "offsets", "camera"),
    ("GET", "percentcompleted", "camera"),
    ("GET", "pixelsizex", "camera"),
    ("GET", "pixelsizey", "camera"),
    ("GET", "readoutmode", "camera"),
    ("PUT", "readoutmode", "camera"),
    ("GET", "readoutmodes", "camera"),
    ("GET", "sensorname", "camera"),
    ("GET", "sensortype", "camera"),
    ("GET", "setccdtemperature", "camera"),
    ("PUT", "setccdtemperature", "camera"),
    ("GET", "startx", "camera"),
    ("PUT", "startx", "camera"),
    ("GET", "starty", "camera"),
    ("PUT", "starty", "camera"),
    ("GET", "subexposureduration", "camera"),
    ("PUT", "subexposureduration", "camera"),
    ("PUT", "abortexposure", "camera"),
    ("PUT", "pulseguide", "camera"),
    ("PUT", "startexposure", "camera"),
    ("PUT", "stopexposure", "camera"),
)


def manifest_rows() -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (spec.http_method, spec.command, spec.category)
        for spec in CAMERA_V4_ENDPOINTS
    )


def test_manifest_matches_the_complete_official_camera_v4_table() -> None:
    assert manifest_rows() == EXPECTED_CAMERA_V4_ENDPOINTS
    assert len(CAMERA_V4_ENDPOINTS) == 88


def test_manifest_entries_are_frozen_typed_endpoint_specs() -> None:
    assert all(type(spec) is EndpointSpec for spec in CAMERA_V4_ENDPOINTS)
    assert EndpointSpec.__dataclass_params__.frozen is True


def test_manifest_has_no_duplicate_method_command_or_category_rows() -> None:
    rows = manifest_rows()

    assert len(rows) == len(set(rows))
    assert len({(method, command) for method, command, _ in rows}) == len(rows)
    assert {category for _, _, category in rows} == {"common", "camera", "image"}


def test_manifest_uses_lowercase_commands_and_supported_http_methods() -> None:
    assert all(command == command.casefold() for _, command, _ in manifest_rows())
    assert {method for method, _, _ in manifest_rows()} == {"GET", "PUT"}
