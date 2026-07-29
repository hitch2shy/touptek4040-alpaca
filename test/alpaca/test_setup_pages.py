from __future__ import annotations

import configparser
from pathlib import Path
from types import SimpleNamespace

import falcon.testing
import pytest

from touptek4040.alpaca.app import create_app


def _configurable_client(coordinator, config_path: Path, host="0.0.0.0"):
    config = SimpleNamespace(
        config_path=config_path,
        server=SimpleNamespace(
            host=host,
            port=11111,
            discovery_port=32227,
        ),
        camera=SimpleNamespace(
            source="toupcam",
            serial="TP241025143218C92B01151AEC40CB7",
            readout_mode="RAW 12-bit",
            gain=100,
        ),
    )
    return falcon.testing.TestClient(create_app(coordinator, config))


def _write_config(path: Path) -> bytes:
    content = (
        "[server]\n"
        "host = 0.0.0.0\n"
        "port = 11111\n"
        "discovery_port = 32227\n"
        "\n"
        "[camera]\n"
        "source = toupcam\n"
        "custom_camera_field = keep-camera\n"
        "\n"
        "[unrelated]\n"
        "custom_value = keep-unrelated\n"
    ).encode("utf-8")
    path.write_bytes(content)
    return content


def test_main_setup_page_is_safe_read_only_html(client):
    response = client.simulate_get("/setup")

    assert response.status_code == 200
    assert response.content_type.startswith("text/html")
    assert "ToupTek 4040 Alpaca Server" in response.text
    assert "Hangzhou ToupTek Photonics Co., Ltd." in response.text
    assert "0.1.0" in response.text
    assert "read-only information page" in response.text
    assert "/setup/v1/camera/0/setup" in response.text
    assert "<form" not in response.text.casefold()
    assert "TP241025143218C92B01151AEC40CB7" not in response.text
    assert "config_path" not in response.text


def test_camera_setup_page_is_explicitly_non_configurable(client):
    response = client.simulate_get("/setup/v1/camera/0/setup")

    assert response.status_code == 200
    assert response.content_type.startswith("text/html")
    assert "ToupTek 4040 Alpaca Camera" in response.text
    assert "sMAX16AM-U3-CL-GPS" in response.text
    assert "not configurable from this page" in response.text
    assert "<form" not in response.text.casefold()
    assert "TP241025143218C92B01151AEC40CB7" not in response.text


def test_setup_html_escapes_dynamic_metadata(client, monkeypatch):
    from touptek4040.alpaca import management

    monkeypatch.setattr(
        management,
        "SERVER_NAME",
        'Server <script>alert("SECRET")</script>',
    )
    monkeypatch.setattr(
        management,
        "MANUFACTURER",
        'Maker"><img src=x onerror=alert(1)>',
    )

    response = client.simulate_get("/setup")

    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "<img" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "&lt;img" in response.text
    assert "&quot;&gt;&lt;img" in response.text


def test_setup_get_does_not_connect_or_touch_camera(disconnected_client, api_camera):
    before = api_camera.connected

    main = disconnected_client.simulate_get("/setup")
    device = disconnected_client.simulate_get("/setup/v1/camera/0/setup")

    assert main.status_code == device.status_code == 200
    assert api_camera.connected is before is False


def test_setup_put_is_not_an_official_configuration_operation(client, api_camera):
    before = api_camera.connected

    response = client.simulate_put(
        "/setup/v1/camera/0/setup",
        json={"serial": "other", "cooler": True},
    )

    assert response.status_code == 405
    assert {
        method.strip()
        for method in response.headers["allow"].split(",")
    } == {"GET", "OPTIONS"}
    assert api_camera.connected is before is True


def test_unsupported_setup_device_paths_are_html_403(client):
    for path in (
        "/setup/v1/telescope/0/setup",
        "/setup/v1/Camera/0/setup",
        "/setup/v1/camera/1/setup",
        "/setup/v1/camera/not-a-number/setup",
    ):
        response = client.simulate_get(path)
        assert response.status_code == 403
        assert response.content_type.startswith("text/html")
        assert "unsupported setup path" in response.text.casefold()


def test_root_setup_get_escapes_configured_values_and_offers_server_form(
    coordinator,
    tmp_path,
):
    config_path = tmp_path / "camera.ini"
    _write_config(config_path)
    client = _configurable_client(
        coordinator,
        config_path,
        host='"><script>alert(1)</script>',
    )

    response = client.simulate_get("/setup")

    assert response.status_code == 200
    assert response.content_type.startswith("text/html")
    assert '<form method="post" action="/setup">' in response.text
    assert "<script>" not in response.text
    assert "&quot;&gt;&lt;script&gt;" in response.text
    assert 'name="port"' in response.text
    assert 'name="discovery_port"' in response.text


@pytest.mark.parametrize("target_kind", ["missing", "directory"])
def test_root_setup_get_with_unusable_config_target_is_read_only(
    coordinator,
    tmp_path,
    target_kind,
):
    config_path = tmp_path / "camera.ini"
    if target_kind == "directory":
        config_path.mkdir()
    client = _configurable_client(coordinator, config_path)

    response = client.simulate_get("/setup")

    assert response.status_code == 200
    assert response.content_type.startswith("text/html")
    assert "read-only" in response.text.casefold()
    assert "<form" not in response.text.casefold()


def test_root_setup_post_atomically_updates_only_server_fields(
    coordinator,
    api_camera,
    tmp_path,
):
    config_path = tmp_path / "camera.ini"
    _write_config(config_path)
    client = _configurable_client(coordinator, config_path)
    before_camera = api_camera.connected

    response = client.simulate_post(
        "/setup",
        body="host=127.0.0.1&port=12345&discovery_port=32228",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    assert response.content_type.startswith("text/html")
    assert "saved" in response.text.casefold()
    assert "restart" in response.text.casefold()
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    assert dict(parser["server"]) == {
        "host": "127.0.0.1",
        "port": "12345",
        "discovery_port": "32228",
    }
    assert parser["camera"]["custom_camera_field"] == "keep-camera"
    assert parser["unrelated"]["custom_value"] == "keep-unrelated"
    assert api_camera.connected is before_camera is False
    assert list(tmp_path.glob("*.tmp")) == []


def test_root_setup_without_config_path_is_read_only_and_forbids_post(
    disconnected_client,
    api_camera,
):
    before_camera = api_camera.connected

    get_response = disconnected_client.simulate_get("/setup")
    post_response = disconnected_client.simulate_post(
        "/setup",
        body="host=127.0.0.1&port=12345&discovery_port=32228",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert get_response.status_code == 200
    assert "<form" not in get_response.text.casefold()
    assert post_response.status_code == 403
    assert post_response.content_type.startswith("text/html")
    assert "read-only" in post_response.text.casefold()
    assert api_camera.connected is before_camera is False


@pytest.mark.parametrize(
    "body",
    [
        "host=&port=12345&discovery_port=32228",
        "host=http%3A%2F%2Fexample.com&port=12345&discovery_port=32228",
        "host=bad_host&port=12345&discovery_port=32228",
        "host=127.0.0.1&port=0&discovery_port=32228",
        "host=127.0.0.1&port=65536&discovery_port=32228",
        "host=127.0.0.1&port=true&discovery_port=32228",
        "host=127.0.0.1&port=12345&discovery_port=-1",
        "host=127.0.0.1&port=12345",
        "host=127.0.0.1&host=0.0.0.0&port=12345&discovery_port=32228",
    ],
)
def test_root_setup_rejects_invalid_form_without_partial_write(
    coordinator,
    tmp_path,
    body,
):
    config_path = tmp_path / "camera.ini"
    original = _write_config(config_path)
    client = _configurable_client(coordinator, config_path)

    response = client.simulate_post(
        "/setup",
        body=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert response.content_type.startswith("text/html")
    assert "invalid server configuration" in response.text.casefold()
    assert config_path.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_root_setup_rejects_malformed_non_form_body_without_write(
    coordinator,
    tmp_path,
):
    config_path = tmp_path / "camera.ini"
    original = _write_config(config_path)
    client = _configurable_client(coordinator, config_path)

    response = client.simulate_post(
        "/setup",
        json=["host", "127.0.0.1"],
    )

    assert response.status_code == 400
    assert response.content_type.startswith("text/html")
    assert config_path.read_bytes() == original


def test_root_setup_missing_config_file_has_stable_error_without_creation(
    coordinator,
    tmp_path,
):
    config_path = tmp_path / "missing.ini"
    client = _configurable_client(coordinator, config_path)

    response = client.simulate_post(
        "/setup",
        body="host=127.0.0.1&port=12345&discovery_port=32228",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 403
    assert response.content_type.startswith("text/html")
    assert "configuration file is unavailable" in response.text.casefold()
    assert config_path.exists() is False
    assert list(tmp_path.iterdir()) == []


def test_root_setup_directory_config_target_is_never_written(
    coordinator,
    tmp_path,
):
    config_path = tmp_path / "camera.ini"
    config_path.mkdir()
    client = _configurable_client(coordinator, config_path)

    response = client.simulate_post(
        "/setup",
        body="host=127.0.0.1&port=12345&discovery_port=32228",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 403
    assert "configuration file is unavailable" in response.text.casefold()
    assert config_path.is_dir()
    assert list(config_path.iterdir()) == []
    assert list(tmp_path.glob("*.tmp")) == []


def test_root_setup_file_disappearing_after_get_is_not_recreated(
    coordinator,
    tmp_path,
):
    config_path = tmp_path / "camera.ini"
    _write_config(config_path)
    client = _configurable_client(coordinator, config_path)
    get_response = client.simulate_get("/setup")
    assert '<form method="post" action="/setup">' in get_response.text

    config_path.unlink()
    post_response = client.simulate_post(
        "/setup",
        body="host=127.0.0.1&port=12345&discovery_port=32228",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert post_response.status_code == 403
    assert "configuration file is unavailable" in post_response.text.casefold()
    assert config_path.exists() is False
    assert list(tmp_path.iterdir()) == []


def test_root_setup_replace_failure_keeps_original_and_removes_temporary_file(
    coordinator,
    tmp_path,
    monkeypatch,
):
    from touptek4040.alpaca import management

    config_path = tmp_path / "camera.ini"
    original = _write_config(config_path)
    client = _configurable_client(coordinator, config_path)

    def fail_replace(source, destination):
        del source, destination
        raise PermissionError("SECRET native filesystem detail")

    monkeypatch.setattr(management.os, "replace", fail_replace)
    response = client.simulate_post(
        "/setup",
        body="host=127.0.0.1&port=12345&discovery_port=32228",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 500
    assert response.content_type.startswith("text/html")
    assert "configuration could not be saved" in response.text.casefold()
    assert "SECRET" not in response.text
    assert config_path.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []
