from __future__ import annotations

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path


class _SelectorInventory(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.classes: set[str] = set()
        self.elements: dict[str, tuple[str, dict[str, str | None]]] = {}

    def handle_starttag(self, tag, attrs) -> None:
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.add(attributes["id"])
            self.elements[attributes["id"]] = (tag, attributes)
        self.classes.update(attributes.get("class", "").split())


def test_viewport_toolbar_and_stage_expose_scoped_navigation_accessibly(web_client):
    """Catches missing controls, implicit submits, or undocumented shortcuts."""
    response = web_client.simulate_get("/ui/")
    inventory = _SelectorInventory()
    inventory.feed(response.text)

    required_ids = {
        "viewport-zoom-out",
        "viewport-zoom",
        "viewport-zoom-in",
        "viewport-fit",
        "viewport-one-to-one",
        "viewport-box-zoom",
        "viewport-layer-host",
        "viewport-selection",
        "viewport-selection-label",
        "viewport-retry",
    }
    assert required_ids <= inventory.ids

    expected_buttons = {
        "viewport-zoom-out": "Zoom out",
        "viewport-zoom-in": "Zoom in",
        "viewport-fit": "Fit image to viewport",
        "viewport-one-to-one": "View image at 100%",
        "viewport-box-zoom": "Box zoom, view only",
        "viewport-retry": "Retry image",
    }
    for element_id, accessible_label in expected_buttons.items():
        tag, attributes = inventory.elements[element_id]
        assert tag == "button"
        assert attributes.get("type") == "button"
        assert attributes.get("aria-label") == accessible_label

    stage_tag, stage_attributes = inventory.elements["image-stage"]
    assert stage_tag == "div"
    assert stage_attributes.get("tabindex") == "0"
    assert stage_attributes.get("aria-label") == (
        "Image viewport. Click to activate. Use the mouse wheel, +, -, 0, and 1 "
        "to zoom; drag to pan; Shift-drag for view-only box zoom; arrow keys "
        "move the inspected pixel."
    )
    assert "View only" in response.text


def test_viewport_error_state_stacks_above_retained_image_layers(web_client):
    """Catches retry controls being painted underneath the last good image."""
    css = web_client.simulate_get("/ui/app.css").text
    layer_rule = re.search(r"\.viewport-layer-host\s*\{(?P<body>[^}]*)\}", css)
    state_rule = re.search(r"\.viewport-state\s*\{(?P<body>[^}]*)\}", css)

    assert layer_rule is not None
    assert state_rule is not None
    assert "z-index: 0" in layer_rule["body"]
    assert "position: relative" in state_rule["body"]
    assert "z-index: 2" in state_rule["body"]


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


def test_viewport_modules_are_allowlisted_with_no_cache_and_csp_support(web_client):
    """Catches blocked viewport imports or display blobs in the shipped console."""
    for path in ("/ui/viewport-math.js", "/ui/image-viewport.js"):
        response = web_client.simulate_get(path)
        assert response.status_code == 200
        assert response.content_type == "text/javascript; charset=utf-8"
        assert response.headers["cache-control"] == "no-cache"

    csp = web_client.simulate_get("/ui/").headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "img-src 'self' data: blob:" in csp


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


def test_reduced_motion_rules_select_the_actual_animated_dom_elements(web_client):
    html = web_client.simulate_get("/ui/").text
    css = web_client.simulate_get("/ui/app.css").text
    inventory = _SelectorInventory()
    inventory.feed(html)
    reduced_motion = re.search(
        r"@media \(prefers-reduced-motion: reduce\)\s*\{(?P<body>.*)\}\s*$",
        css,
        re.DOTALL,
    )

    assert reduced_motion is not None
    selectors = set(
        re.findall(r"([.#][a-zA-Z0-9_-]+)\s*\{", reduced_motion["body"])
    )
    assert selectors == {".skeleton-block", "#progress-fill"}
    assert "skeleton-block" in inventory.classes
    assert "progress-fill" in inventory.ids
