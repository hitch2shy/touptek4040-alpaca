from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


MATH_MODULE = (
    Path(__file__).parents[2] / "touptek4040" / "web" / "viewport-math.js"
).as_uri()


def run_math(expression: str):
    script = f"""
      import * as math from {json.dumps(MATH_MODULE)};
      const result = ({expression});
      process.stdout.write(JSON.stringify(result));
    """
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_math_fits_and_clamps_scale_while_preserving_pointer_coordinate():
    """Catches fit, scale-bound, or pointer-anchoring regressions."""
    result = run_math("""(() => {
      const fit = math.fitView(4096, 4096, 1024, 768);
      const before = math.screenToImage(fit, 123, 456, 4096, 4096, 1024, 768);
      const zoomed = math.zoomAt(fit, 1, 123, 456, 4096, 4096, 1024, 768);
      const after = math.screenToImage(zoomed, 123, 456, 4096, 4096, 1024, 768);
      return {
        fit,
        before,
        after,
        minimum: math.zoomAt(fit, -100, 512, 384, 4096, 4096, 1024, 768),
        maximum: math.zoomAt(fit, 100, 512, 384, 4096, 4096, 1024, 768),
      };
    })()""")

    assert result["fit"] == {"scale": 0.1875, "centerX": 2048, "centerY": 2048}
    assert result["before"] == pytest.approx(result["after"])
    assert result["minimum"]["scale"] == 0.1875
    assert result["maximum"]["scale"] == 16


def test_math_panning_stays_within_image_edges():
    """Catches panning that would reveal empty space beyond the image."""
    result = run_math("""(() => {
      const fit = math.fitView(4096, 4096, 1024, 768);
      const zoomed = math.zoomAt(fit, 1, 512, 384, 4096, 4096, 1024, 768);
      const northwest = math.panBy(zoomed, 100000, 100000, 4096, 4096, 1024, 768);
      const southeast = math.panBy(zoomed, -100000, -100000, 4096, 4096, 1024, 768);
      return {
        northwest: math.visibleRect(northwest, 4096, 4096, 1024, 768),
        southeast: math.visibleRect(southeast, 4096, 4096, 1024, 768),
      };
    })()""")

    assert result["northwest"] == {
        "x": 0,
        "y": 0,
        "width": pytest.approx(1024 / 0.375),
        "height": pytest.approx(768 / 0.375),
    }
    assert result["southeast"] == {
        "x": pytest.approx(4096 - 1024 / 0.375),
        "y": pytest.approx(4096 / 2),
        "width": pytest.approx(1024 / 0.375),
        "height": pytest.approx(768 / 0.375),
    }


def test_math_normalizes_reverse_selection_to_an_in_bounds_exclusive_rect():
    """Catches selection directions that produce different source rectangles."""
    result = run_math("""(() => {
      const fit = math.fitView(4096, 4096, 1024, 768);
      const one = math.zoomAt(fit, 1, 512, 384, 4096, 4096, 1024, 768);
      return {
        forward: math.selectionToImageRect(
          one, {x: 300, y: 200}, {x: 700, y: 500}, 4096, 4096, 1024, 768,
        ),
        reversed: math.selectionToImageRect(
          one, {x: 700, y: 500}, {x: 300, y: 200}, 4096, 4096, 1024, 768,
        ),
        clamped: math.selectionToImageRect(
          one, {x: -1000, y: -1000}, {x: 2000, y: 2000}, 4096, 4096, 1024, 768,
        ),
      };
    })()""")

    assert result["forward"] == result["reversed"]
    assert result["forward"] == {"x": 2165, "y": 1557, "width": 1067, "height": 801}
    assert result["clamped"] == {"x": 0, "y": 0, "width": 4096, "height": 4096}


def test_math_plans_nonoverlapping_segments_that_cover_the_visible_crop():
    """Catches segment gaps, duplicate pixels, or source edges above the limit."""
    result = run_math("""(() => {
      const view = {scale: 1, centerX: 2048, centerY: 2048};
      return {
        visible: math.visibleRect(view, 4096, 4096, 3000, 1400),
        segments: math.planCropSegments(view, 4096, 4096, 3000, 1400, 2048),
      };
    })()""")

    assert result["visible"] == {"x": 548, "y": 1348, "width": 3000, "height": 1400}
    assert result["segments"] == [
        {
            "x": 548,
            "y": 1348,
            "width": 2048,
            "height": 1400,
            "left": 0,
            "top": 0,
            "displayWidth": 2048,
            "displayHeight": 1400,
        },
        {
            "x": 2596,
            "y": 1348,
            "width": 952,
            "height": 1400,
            "left": 2048,
            "top": 0,
            "displayWidth": 952,
            "displayHeight": 1400,
        },
    ]


def test_math_tiles_rows_and_columns_with_integer_segment_edges():
    """Catches two-dimensional segment gaps, overlap, and fractional crops."""
    result = run_math("""(() => {
      const view = {scale: 1, centerX: 2048, centerY: 2048};
      let fractionalEdgeIsRejected = false;
      try {
        math.planCropSegments(view, 4096, 4096, 10, 10, 0.5);
      } catch (error) {
        fractionalEdgeIsRejected = error instanceof TypeError;
      }
      return {
        segments: math.planCropSegments(view, 4096, 4096, 3000, 3000, 2048),
        fractionalEdgeIsRejected,
      };
    })()""")

    assert result["fractionalEdgeIsRejected"] is True
    assert result["segments"] == [
        {"x": 548, "y": 548, "width": 2048, "height": 2048, "left": 0, "top": 0, "displayWidth": 2048, "displayHeight": 2048},
        {"x": 2596, "y": 548, "width": 952, "height": 2048, "left": 2048, "top": 0, "displayWidth": 952, "displayHeight": 2048},
        {"x": 548, "y": 2596, "width": 2048, "height": 952, "left": 0, "top": 2048, "displayWidth": 2048, "displayHeight": 952},
        {"x": 2596, "y": 2596, "width": 952, "height": 952, "left": 2048, "top": 2048, "displayWidth": 952, "displayHeight": 952},
    ]


def test_math_rejects_invalid_dimensions():
    """Catches invalid dimensions silently entering viewport calculations."""
    result = run_math("""(() => {
      const calls = [
        () => math.fitView(0, 4096, 1024, 768),
        () => math.visibleRect({scale: 1, centerX: 1, centerY: 1}, 4096, 4096, Infinity, 768),
        () => math.planCropSegments({scale: 1, centerX: 1, centerY: 1}, 4096, 4096, 100, 100, 0),
      ];
      return calls.map((call) => {
        try { call(); return false; } catch (error) { return error instanceof TypeError; }
      });
    })()""")

    assert result == [True, True, True]
