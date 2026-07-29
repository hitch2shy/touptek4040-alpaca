from __future__ import annotations

import json
import subprocess
from pathlib import Path


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


def test_generation_contract_clamps_raw8_stretch_and_pins_every_image_url():
    module = (
        Path(__file__).parents[2]
        / "touptek4040"
        / "web"
        / "generation-contract.js"
    ).as_uri()
    script = f"""
      const contract = await import({json.dumps(module)});
      const output = {{
        raw8Default: contract.normalizeStretch(255, 0, 4095),
        raw8Manual: contract.normalizeStretch(255, 200, 4095),
        raw8Auto: contract.autoStretch(
          255,
          {{mean: 200, standardDeviation: 30}},
        ),
        viewport: contract.generationUrl(
          "/web/v1/viewport.png",
          7,
          {{white: 255}},
        ),
        stats: contract.generationUrl("/web/v1/statistics", 7),
        pixel: contract.generationUrl(
          "/web/v1/pixel",
          7,
          {{x: 1, y: 2}},
        ),
        fits: contract.generationUrl("/web/v1/image.fits", 7),
        readout: contract.authoritativeReadout({{
          image: {{readoutMode: "RAW 8-bit"}},
          settings: {{readoutMode: "RAW 12-bit"}},
        }}),
      }};
      process.stdout.write(JSON.stringify(output));
    """

    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "raw8Default": {"black": 0, "white": 255},
        "raw8Manual": {"black": 200, "white": 255},
        "raw8Auto": {"black": 140, "white": 255},
        "viewport": "/web/v1/viewport.png?generation=7&white=255",
        "stats": "/web/v1/statistics?generation=7",
        "pixel": "/web/v1/pixel?generation=7&x=1&y=2",
        "fits": "/web/v1/image.fits?generation=7",
        "readout": "RAW 8-bit",
    }
