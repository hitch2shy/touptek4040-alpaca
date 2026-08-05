from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


MATH_MODULE = (
    Path(__file__).parents[2] / "touptek4040" / "web" / "viewport-math.js"
).as_uri()
VIEWPORT_MODULE = (
    Path(__file__).parents[2] / "touptek4040" / "web" / "image-viewport.js"
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


def run_viewport(body: str):
    """Run the viewport controller against a deliberately small fake DOM."""
    script = f"""
      import * as viewport from {json.dumps(VIEWPORT_MODULE)};

      function element() {{
        const listeners = new Map();
        return {{
          hidden: false,
          textContent: "",
          style: {{}},
          dataset: {{}},
          children: [],
          parentNode: null,
          clientWidth: 1024,
          clientHeight: 768,
          append(...nodes) {{
            nodes.forEach((node) => {{ node.parentNode = this; this.children.push(node); }});
          }},
          replaceChildren(...nodes) {{
            this.children.forEach((node) => {{ node.parentNode = null; }});
            this.children = [];
            this.append(...nodes);
          }},
          addEventListener(name, handler) {{ listeners.set(name, handler); }},
          removeEventListener(name, handler) {{
            if (listeners.get(name) === handler) listeners.delete(name);
          }},
          dispatch(name) {{ listeners.get(name)?.({{preventDefault() {{}}}}); }},
          getBoundingClientRect() {{ return {{width: 1024, height: 768}}; }},
        }};
      }}

      globalThis.document = {{createElement: () => element()}};
      const elements = {{
        stage: element(),
        layerHost: element(),
        selection: element(),
        selectionLabel: element(),
        zoomOutput: element(),
        retry: element(),
        zoomOut: element(), zoomIn: element(), fit: element(),
        oneToOne: element(), boxZoom: element(), zoom: element(),
      }};
      {body}
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


def test_math_never_exceeds_max_scale_for_a_tiny_image():
    """Catches tiny-image fits leaking a scale above the public maximum."""
    result = run_math("""(() => {
      const fit = math.fitView(10, 10, 1024, 768);
      return {
        fit,
        zoomed: math.zoomAt(fit, 1, 160, 160, 10, 10, 1024, 768),
        panned: math.panBy(fit, 100, 100, 10, 10, 1024, 768),
        screen: math.screenToImage(fit, 160, 160, 10, 10, 1024, 768),
        selection: math.selectionToImageRect(
          fit, {x: 0, y: 0}, {x: 160, y: 160}, 10, 10, 1024, 768,
        ),
        segments: math.planCropSegments(fit, 10, 10, 1024, 768),
      };
    })()""")

    assert result["fit"]["scale"] == 16
    assert result["zoomed"]["scale"] == 16
    assert result["panned"]["scale"] == 16
    assert result["screen"] == {"x": 10, "y": 10}
    assert result["selection"] == {"x": 0, "y": 0, "width": 10, "height": 10}
    assert result["segments"] == [
        {
            "x": 0,
            "y": 0,
            "width": 10,
            "height": 10,
            "left": 0,
            "top": 0,
            "displayWidth": 160,
            "displayHeight": 160,
        },
    ]


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


def test_viewport_requests_publish_only_the_latest_complete_crop_layers():
    """Catches stale crop publication, missing aborts, or crop URL regressions."""
    result = run_viewport("""\
      const pending = [];
      const revoked = [];
      const fetchBlob = (url, signal) => new Promise((resolve, reject) => {
        pending.push({url, signal, resolve, reject});
      });
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob,
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: (url) => revoked.push(url),
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      controller.setStretch({black: 100, white: 2000, gamma: 1.2});
      const first = controller.refresh();
      const firstRequestCount = pending.length;
      const second = controller.refresh();
      pending.slice(0, firstRequestCount).forEach((request, index) =>
        request.resolve({id: `stale-${index}`}),
      );
      pending.slice(firstRequestCount).forEach((request, index) =>
        request.resolve({id: `latest-${index}`}),
      );
      await Promise.all([first, second]);
      const query = Object.fromEntries(new URL(
        `http://test${pending[firstRequestCount].url}`,
      ).searchParams);
      process.stdout.write(JSON.stringify({
        firstAborted: pending.slice(0, firstRequestCount).every(
          (request) => request.signal.aborted,
        ),
        query,
        layers: elements.layerHost.children.map((layer) => layer.src),
        revoked,
      }));
    """)

    assert result == {
        "firstAborted": True,
        "query": {
            "generation": "7",
            "x": "0",
            "y": "0",
            "width": "2048",
            "height": "2048",
            "black": "100",
            "white": "2000",
            "gamma": "1.2",
            "maxWidth": "384",
            "maxHeight": "384",
        },
        "layers": [
            "blob:latest-0", "blob:latest-1", "blob:latest-2", "blob:latest-3",
        ],
        "revoked": [],
    }


def test_viewport_generation_transitions_preserve_or_reset_the_view():
    """Catches generation changes losing a compatible view or retaining an invalid one."""
    result = run_viewport("""\
      let blobId = 0;
      const requests = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async (url) => {
          requests.push(url);
          return {id: ++blobId};
        },
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: () => {},
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      controller.setStretch({black: 0, white: 4095, gamma: 1});
      await controller.oneToOne();
      controller.setGeneration(8, {width: 4096, height: 4096});
      await controller.refresh();
      elements.selection.hidden = false;
      controller.setGeneration(9, {width: 2048, height: 1024});
      await controller.refresh();
      const crop = (url) => Object.fromEntries(
        new URL(`http://test${url}`).searchParams,
      );
      process.stdout.write(JSON.stringify({
        oneToOne: crop(requests[0]),
        sameShape: crop(requests[1]),
        changedShape: crop(requests[2]),
        selectionHidden: elements.selection.hidden,
      }));
    """)

    assert result == {
        "oneToOne": {
            "generation": "7", "x": "1536", "y": "1664",
            "width": "1024", "height": "768", "black": "0",
            "white": "4095", "gamma": "1", "maxWidth": "1024",
            "maxHeight": "768",
        },
        "sameShape": {
            "generation": "8", "x": "1536", "y": "1664",
            "width": "1024", "height": "768", "black": "0",
            "white": "4095", "gamma": "1", "maxWidth": "1024",
            "maxHeight": "768",
        },
        "changedShape": {
            "generation": "9", "x": "0", "y": "0",
            "width": "2048", "height": "1024", "black": "0",
            "white": "4095", "gamma": "1", "maxWidth": "1024",
            "maxHeight": "512",
        },
        "selectionHidden": True,
    }


def test_viewport_errors_and_destroy_keep_layers_stable_and_release_urls():
    """Catches conflict/error routing, leaked URLs, or publication after teardown."""
    result = run_viewport("""\
      const pending = [];
      const revoked = [];
      const conflicts = [];
      const errors = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: (url, signal) => new Promise((resolve, reject) => {
          pending.push({url, signal, resolve, reject});
        }),
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: (url) => revoked.push(url),
        onGenerationConflict: (error) => conflicts.push(error.status),
        onError: (error) => errors.push(error.message),
      });
      controller.setGeneration(7, {width: 1024, height: 768});
      const loaded = controller.refresh();
      pending[0].resolve({id: "owned"});
      await loaded;
      const conflict = controller.refresh();
      const conflictError = new Error("changed");
      conflictError.status = 409;
      pending[1].reject(conflictError);
      await conflict;
      const failed = controller.refresh();
      pending[2].reject(new Error("network lost"));
      await failed;
      const retryBeforeDestroy = elements.retry.hidden;
      const late = controller.refresh();
      controller.destroy();
      pending[3].resolve({id: "late"});
      await late;
      process.stdout.write(JSON.stringify({
        conflicts,
        errors,
        retryBeforeDestroy,
        layers: elements.layerHost.children.map((layer) => layer.src),
        revoked,
        lateAborted: pending[3].signal.aborted,
      }));
    """)

    assert result == {
        "conflicts": [409],
        "errors": ["network lost"],
        "retryBeforeDestroy": False,
        "layers": [],
        "revoked": ["blob:owned"],
        "lateAborted": True,
    }


def test_viewport_dimension_changes_keep_only_in_range_inspections():
    """Catches image-shape transitions discarding valid or retaining invalid inspection."""
    result = run_viewport("""\
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async () => ({id: "unused"}),
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: () => {{}},
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      elements.selection.hidden = false;
      elements.selection.dataset.x = "100";
      elements.selection.dataset.y = "100";
      controller.setGeneration(8, {width: 2048, height: 1024});
      const validInspectionRemains = !elements.selection.hidden;
      elements.selection.dataset.x = "3000";
      elements.selection.dataset.y = "2000";
      controller.setGeneration(9, {width: 1024, height: 768});
      process.stdout.write(JSON.stringify({
        validInspectionRemains,
        invalidInspectionCleared: elements.selection.hidden,
      }));
    """)

    assert result == {
        "validInspectionRemains": True,
        "invalidInspectionCleared": True,
    }


def test_viewport_same_generation_dimension_change_supersedes_old_crops():
    """Catches stale layers publishing after the image shape changes in one generation."""
    result = run_viewport("""\
      const pending = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: (url, signal) => new Promise((resolve) =>
          pending.push({url, signal, resolve}),
        ),
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: () => {},
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      const oldRender = controller.refresh();
      controller.setGeneration(7, {width: 2048, height: 1024});
      pending.forEach((request, index) => request.resolve({id: index}));
      await oldRender;
      process.stdout.write(JSON.stringify({
        aborted: pending.every((request) => request.signal.aborted),
        layers: elements.layerHost.children.length,
      }));
    """)

    assert result == {"aborted": True, "layers": 0}


def test_viewport_terminal_segment_failure_aborts_its_siblings():
    """Catches multi-segment render failures that leave sibling crop requests alive."""
    result = run_viewport("""\
      const pending = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: (url, signal) => new Promise((resolve, reject) =>
          pending.push({url, signal, resolve, reject}),
        ),
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: () => {},
        onError: () => {},
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      const render = controller.refresh();
      pending[0].reject(new Error("network lost"));
      await render;
      process.stdout.write(JSON.stringify({
        abortedSiblings: pending.slice(1).every((request) => request.signal.aborted),
      }));
    """)

    assert result == {"abortedSiblings": True}
