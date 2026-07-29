from __future__ import annotations


def test_ui_is_the_touptek_image_first_console_with_no_legacy_device_copy(
    web_client,
):
    response = web_client.simulate_get("/ui/")

    assert response.status_code == 200
    assert response.content_type == "text/html; charset=utf-8"
    assert "ToupTek 4040" in response.text
    assert "4096 × 4096" in response.text
    assert "RAW 12-bit" in response.text
    assert "Trusted LAN" in response.text
    for legacy in ("COSMOS-66", "COSMOS66", "PICam", "8120", "HDR 18"):
        assert legacy not in response.text


def test_ui_assets_are_no_build_modules_with_safe_types_and_cache_policy(web_client):
    script = web_client.simulate_get("/ui/app.js")
    stylesheet = web_client.simulate_get("/ui/app.css")

    assert script.status_code == 200
    assert script.content_type == "text/javascript; charset=utf-8"
    assert script.headers["cache-control"] == "no-cache"
    assert stylesheet.status_code == 200
    assert stylesheet.content_type == "text/css; charset=utf-8"
    assert stylesheet.headers["x-content-type-options"] == "nosniff"


def test_static_asset_paths_cannot_escape_the_embedded_web_root(web_client):
    response = web_client.simulate_get("/ui/%2e%2e/%2e%2e/PRODUCT.md")

    assert response.status_code in {400, 404}
    assert "Product Purpose" not in response.text
