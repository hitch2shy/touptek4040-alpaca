from __future__ import annotations

def test_main_setup_page_is_safe_read_only_html(client):
    response = client.simulate_get("/setup")

    assert response.status_code == 200
    assert response.content_type.startswith("text/html")
    assert "ToupTek 4040 Alpaca Server" in response.text
    assert "Hangzhou ToupTek Photonics Co., Ltd." in response.text
    assert "0.1.0" in response.text
    assert "read-only information page" in response.text
    assert "/setup/v1/camera/0/setup" in response.text
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
