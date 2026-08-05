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
VIEWER_MODULE = (
    Path(__file__).parents[2] / "touptek4040" / "web" / "image-viewer.js"
).as_uri()
APP_MODULE = (
    Path(__file__).parents[2] / "touptek4040" / "web" / "app.js"
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

      function element(tagName = "DIV") {{
        const listeners = new Map();
        return {{
          tagName,
          type: "",
          isContentEditable: false,
          hidden: false,
          textContent: "",
          style: {{}},
          dataset: {{}},
          attributes: {{}},
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
          dispatch(name, supplied = {{}}) {{
            const event = {{
              target: this,
              currentTarget: this,
              button: 0,
              buttons: 0,
              pointerId: 1,
              clientX: 0,
              clientY: 0,
              deltaY: 0,
              key: "",
              code: "",
              shiftKey: false,
              ctrlKey: false,
              altKey: false,
              metaKey: false,
              defaultPrevented: false,
              preventDefault() {{ this.defaultPrevented = true; }},
              ...supplied,
            }};
            event.result = listeners.get(name)?.(event);
            return event;
          }},
          focus() {{
            globalThis.document.activeElement = this;
            this.dispatch("focus");
          }},
          setPointerCapture(pointerId) {{ this.capturedPointer = pointerId; }},
          releasePointerCapture(pointerId) {{
            if (this.capturedPointer === pointerId) this.capturedPointer = null;
          }},
          hasPointerCapture(pointerId) {{ return this.capturedPointer === pointerId; }},
          setAttribute(name, value) {{ this.attributes[name] = String(value); }},
          removeAttribute(name) {{ delete this.attributes[name]; }},
          getBoundingClientRect() {{
            return {{
              left: 0, top: 0, right: this.clientWidth, bottom: this.clientHeight,
              width: this.clientWidth, height: this.clientHeight,
            }};
          }},
        }};
      }}

      globalThis.document = {{activeElement: null, createElement: (tag) => element(tag.toUpperCase())}};
      const elements = {{
        stage: element(),
        layerHost: element(),
        selection: element(),
        selectionLabel: element(),
        zoomOutput: element(),
        retry: element(),
        zoomOut: element(), zoomIn: element(), fit: element(),
        oneToOne: element(), boxZoom: element(), zoom: element(),
        pixelCursor: element(),
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


def run_viewer(body: str):
    """Run the composed image viewer with real modules and a small fake DOM."""
    script = f"""
      function element(tagName = "DIV") {{
        const listeners = new Map();
        return {{
          tagName, type: "", value: "", max: "", hidden: false,
          textContent: "", style: {{}}, dataset: {{}}, attributes: {{}},
          children: [], clientWidth: 1024, clientHeight: 768,
          append(...nodes) {{ this.children.push(...nodes); }},
          replaceChildren(...nodes) {{ this.children = [...nodes]; }},
          addEventListener(name, handler) {{ listeners.set(name, handler); }},
          removeEventListener(name, handler) {{
            if (listeners.get(name) === handler) listeners.delete(name);
          }},
          dispatch(name, supplied = {{}}) {{
            const event = {{
              target: this, currentTarget: this, button: 0, buttons: 0,
              pointerId: 1, clientX: 0, clientY: 0, deltaY: 0,
              key: "", code: "", shiftKey: false, ctrlKey: false,
              altKey: false, metaKey: false, defaultPrevented: false,
              preventDefault() {{ this.defaultPrevented = true; }},
              ...supplied,
            }};
            event.result = listeners.get(name)?.(event);
            return event;
          }},
          focus() {{ globalThis.document.activeElement = this; }},
          setPointerCapture(pointerId) {{ this.capturedPointer = pointerId; }},
          releasePointerCapture(pointerId) {{
            if (this.capturedPointer === pointerId) this.capturedPointer = null;
          }},
          hasPointerCapture(pointerId) {{ return this.capturedPointer === pointerId; }},
          setAttribute(name, value) {{ this.attributes[name] = String(value); }},
          removeAttribute(name) {{ delete this.attributes[name]; }},
          getBoundingClientRect() {{
            return {{left: 0, top: 0, right: 1024, bottom: 768, width: 1024, height: 768}};
          }},
          getContext() {{
            return {{clearRect() {{}}, fillRect() {{}}, fillStyle: ""}};
          }},
        }};
      }}
      const ids = [
        "black-point", "white-point", "gamma", "black-value", "white-value",
        "gamma-value", "auto-stretch", "refresh-analysis", "generation-label",
        "fits-link", "image-stage", "viewport-layer-host", "viewport-selection",
        "viewport-selection-label", "viewport-zoom", "viewport-retry",
        "viewport-zoom-out", "viewport-zoom-in", "viewport-fit",
        "viewport-one-to-one", "viewport-box-zoom", "pixel-cursor", "image-empty",
        "image-loading", "image-error", "image-error-copy", "pixel-readout",
        "stat-min", "stat-max", "stat-mean", "stat-sigma", "stat-fwhm-x",
        "stat-fwhm-y", "histogram-range", "histogram",
      ];
      const elements = Object.fromEntries(ids.map((id) => [id, element()]));
      elements["black-point"].value = "0";
      elements["white-point"].value = "4095";
      elements.gamma.value = "1";
      elements.histogram.width = 288;
      elements.histogram.height = 116;
      globalThis.document = {{
        activeElement: null,
        getElementById: (id) => elements[id],
        createElement: (tag) => element(tag.toUpperCase()),
      }};
      globalThis.URL.createObjectURL = (blob) => `blob:${{blob.id}}`;
      globalThis.URL.revokeObjectURL = () => {{}};
      const viewer = await import({json.dumps(VIEWER_MODULE)});
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
      const before = math.screenToImage(fit, 512, 456, 4096, 4096, 1024, 768);
      const zoomed = math.zoomAt(fit, 1, 512, 456, 4096, 4096, 1024, 768);
      const after = math.screenToImage(zoomed, 512, 456, 4096, 4096, 1024, 768);
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
        screen: math.screenToImage(fit, 512, 384, 10, 10, 1024, 768),
        selection: math.selectionToImageRect(
          fit, {x: 432, y: 304}, {x: 592, y: 464}, 10, 10, 1024, 768,
        ),
        segments: math.planCropSegments(fit, 10, 10, 1024, 768),
      };
    })()""")

    assert result["fit"]["scale"] == 16
    assert result["zoomed"]["scale"] == 16
    assert result["panned"]["scale"] == 16
    assert result["screen"] == {"x": 5, "y": 5}
    assert result["selection"] == {"x": 0, "y": 0, "width": 10, "height": 10}
    assert result["segments"] == [
        {
            "x": 0,
            "y": 0,
            "width": 10,
            "height": 10,
            "left": 432,
            "top": 304,
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
    assert result["forward"] == {"x": 1482, "y": 1557, "width": 1068, "height": 801}
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


def test_math_fit_uses_one_segment_when_display_output_is_within_the_bound():
    """Catches source-size tiling of a downsampled fit that needs one response."""
    result = run_math("""(() => {
      const fit = math.fitView(4096, 4096, 1024, 768);
      return math.planCropSegments(fit, 4096, 4096, 1024, 768, 2048);
    })()""")

    assert result == [
        {
            "x": 0,
            "y": 0,
            "width": 4096,
            "height": 4096,
            "left": 128,
            "top": 0,
            "displayWidth": 768,
            "displayHeight": 768,
        }
    ]


def test_math_centers_letterboxed_display_and_maps_its_visual_center():
    """Catches absolute crop layers drifting to the stage's top-left corner."""
    result = run_math("""(() => {
      const fit = math.fitView(4096, 4096, 1024, 768);
      return {
        display: math.displayRect(fit, 4096, 4096, 1024, 768),
        center: math.screenToImage(fit, 512, 384, 4096, 4096, 1024, 768),
      };
    })()""")

    assert result == {
        "display": {"left": 128, "top": 0, "width": 768, "height": 768},
        "center": {"x": 2048, "y": 2048},
    }


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
                "width": "4096",
                "height": "4096",
            "black": "100",
            "white": "2000",
            "gamma": "1.2",
                "maxWidth": "768",
                "maxHeight": "768",
            },
            "layers": ["blob:latest-0"],
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
      const validInspection = {...elements.selection.dataset};
      elements.selection.dataset.x = "3000";
      elements.selection.dataset.y = "2000";
      controller.setGeneration(9, {width: 1024, height: 768});
      process.stdout.write(JSON.stringify({
        validInspection,
        invalidInspection: elements.selection.dataset,
        invalidMarkerHidden: elements.pixelCursor.hidden,
      }));
    """)

    assert result == {
        "validInspection": {"x": "100", "y": "100"},
        "invalidInspection": {},
        "invalidMarkerHidden": True,
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
      elements.stage.clientWidth = 3000;
      elements.stage.clientHeight = 3000;
      controller.setGeneration(7, {width: 4096, height: 4096});
      const render = controller.oneToOne();
      pending[0].reject(new Error("network lost"));
      await render;
      process.stdout.write(JSON.stringify({
        requestCount: pending.length,
        abortedSiblings: pending.slice(1).every((request) => request.signal.aborted),
      }));
    """)

    assert result == {"requestCount": 4, "abortedSiblings": True}


def test_viewport_null_generation_clears_the_selection_label():
    """Catches a null generation leaving stale view-only selection text visible."""
    result = run_viewport("""\
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async () => ({id: "unused"}),
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: () => {},
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      elements.selection.hidden = false;
      elements.selection.dataset.x = "10";
      elements.selection.dataset.y = "20";
      elements.selectionLabel.hidden = false;
      controller.setGeneration(null);
      process.stdout.write(JSON.stringify({
        selectionHidden: elements.selection.hidden,
        selectionLabelHidden: elements.selectionLabel.hidden,
        dataset: elements.selection.dataset,
      }));
    """)

    assert result == {
        "selectionHidden": True,
        "selectionLabelHidden": True,
        "dataset": {},
    }


def test_viewport_wheel_gesture_is_scoped_and_preserves_the_pointer_coordinate():
    """Catches page-wheel interception or zooming away from the pointed pixel."""
    result = run_viewport("""\
      const requests = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async (url) => { requests.push(url); return {id: requests.length}; },
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: () => {},
      });
      controller.setGeneration(7, {width: 4096, height: 3072});
      const inactive = elements.stage.dispatch("wheel", {
        clientX: 240, clientY: 300, deltaY: -100,
      });
      const initialZoom = elements.zoomOutput.textContent;
      elements.stage.focus();
      const active = elements.stage.dispatch("wheel", {
        clientX: 240, clientY: 300, deltaY: -100,
      });
      await active.result;
      const query = Object.fromEntries(new URL(
        `http://test${requests[0]}`,
      ).searchParams);
      const beforeScale = 0.25;
      const afterScale = 0.3125;
      const before = {x: 240 / beforeScale, y: 300 / beforeScale};
      const after = {
        x: Number(query.x) + 240 / afterScale,
        y: Number(query.y) + 300 / afterScale,
      };
      process.stdout.write(JSON.stringify({
        inactivePrevented: inactive.defaultPrevented,
        activePrevented: active.defaultPrevented,
        initialZoom,
        finalZoom: elements.zoomOutput.textContent,
        coordinateDelta: {x: Math.abs(before.x - after.x), y: Math.abs(before.y - after.y)},
      }));
    """)

    assert result["inactivePrevented"] is False
    assert result["activePrevented"] is True
    assert result["initialZoom"] == "25%"
    assert result["finalZoom"] == "31%"
    assert result["coordinateDelta"]["x"] < 1
    assert result["coordinateDelta"]["y"] < 1


def test_viewport_shortcuts_and_toolbar_apply_the_same_zoom_transitions():
    """Catches missing visible equivalents or inconsistent keyboard zoom states."""
    result = run_viewport("""\
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async () => ({id: "frame"}),
        makeObjectUrl: () => "blob:frame",
        revokeObjectUrl: () => {},
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      elements.stage.focus();
      const press = async (target, name, supplied = {}) => {
        const event = target.dispatch(name, supplied);
        await event.result;
        return {zoom: elements.zoomOutput.textContent, prevented: event.defaultPrevented};
      };
      const keyboardIn = await press(elements.stage, "keydown", {key: "+", code: "Equal"});
      const keyboardOut = await press(elements.stage, "keydown", {key: "-", code: "Minus"});
      const one = await press(elements.stage, "keydown", {key: "1", code: "Digit1"});
      const fit = await press(elements.stage, "keydown", {key: "0", code: "Digit0"});
      const buttonIn = await press(elements.zoomIn, "click");
      const buttonOut = await press(elements.zoomOut, "click");
      const buttonOne = await press(elements.oneToOne, "click");
      const buttonFit = await press(elements.fit, "click");
      const numpadIn = await press(elements.stage, "keydown", {key: "Add", code: "NumpadAdd"});
      const numpadOut = await press(elements.stage, "keydown", {key: "Subtract", code: "NumpadSubtract"});
      process.stdout.write(JSON.stringify({
        keyboardIn, keyboardOut, one, fit,
        buttonIn, buttonOut, buttonOne, buttonFit,
        numpadIn, numpadOut,
      }));
    """)

    assert result == {
        "keyboardIn": {"zoom": "23%", "prevented": True},
        "keyboardOut": {"zoom": "19%", "prevented": True},
        "one": {"zoom": "100%", "prevented": True},
        "fit": {"zoom": "19%", "prevented": True},
        "buttonIn": {"zoom": "23%", "prevented": False},
        "buttonOut": {"zoom": "19%", "prevented": False},
        "buttonOne": {"zoom": "100%", "prevented": False},
        "buttonFit": {"zoom": "19%", "prevented": False},
        "numpadIn": {"zoom": "23%", "prevented": True},
        "numpadOut": {"zoom": "19%", "prevented": True},
    }


def test_viewport_double_click_toggles_fit_and_one_to_one_around_pointer():
    """Catches double-click zoom losing the clicked original coordinate."""
    result = run_viewport("""\
      const requests = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async (url) => { requests.push(url); return {id: requests.length}; },
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: () => {},
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      elements.stage.focus();
      const first = elements.stage.dispatch("dblclick", {clientX: 240, clientY: 300});
      await first.result;
      const oneQuery = Object.fromEntries(new URL(`http://test${requests[0]}`).searchParams);
      const second = elements.stage.dispatch("dblclick", {clientX: 240, clientY: 300});
      await second.result;
      const clickedBefore = {x: (240 - 128) / 0.1875, y: 300 / 0.1875};
      const clickedAfter = {
        x: Number(oneQuery.x) + 240,
        y: Number(oneQuery.y) + 300,
      };
      process.stdout.write(JSON.stringify({
        firstPrevented: first.defaultPrevented,
        secondPrevented: second.defaultPrevented,
        clickedBefore,
        clickedAfter,
        finalZoom: elements.zoomOutput.textContent,
      }));
    """)

    assert result["firstPrevented"] is True
    assert result["secondPrevented"] is True
    assert result["clickedAfter"] == pytest.approx(result["clickedBefore"], abs=1)
    assert result["finalZoom"] == "19%"


def test_viewport_shortcuts_ignore_browser_modifiers_and_control_targets():
    """Catches viewport shortcuts stealing browser or form-control input."""
    result = run_viewport("""\
      const requests = [];
      const inspected = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async (url) => { requests.push(url); return {id: "frame"}; },
        makeObjectUrl: () => "blob:frame",
        revokeObjectUrl: () => {},
        onInspect: (x, y) => inspected.push({x, y}),
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      elements.stage.focus();
      const keyCases = [
        {key: "+", ctrlKey: true},
        {key: "-", altKey: true},
        {key: "0", metaKey: true},
        {key: "+", target: {...element("INPUT"), type: "range"}},
        {key: "-", target: element("SELECT")},
        {key: "1", target: element("TEXTAREA")},
        {key: "0", target: element("BUTTON")},
        {key: "+", target: {...element("DIV"), isContentEditable: true}},
      ];
      const keyOutcomes = [];
      for (const supplied of keyCases) {
        const event = elements.stage.dispatch("keydown", supplied);
        await event.result;
        keyOutcomes.push({prevented: event.defaultPrevented, zoom: elements.zoomOutput.textContent});
      }
      const wheelOutcomes = [];
      for (const supplied of [
        {ctrlKey: true}, {altKey: true}, {metaKey: true},
        {target: {...element("INPUT"), type: "range"}},
      ]) {
        const event = elements.stage.dispatch("wheel", {
          clientX: 400, clientY: 300, deltaY: -100, ...supplied,
        });
        await event.result;
        wheelOutcomes.push({prevented: event.defaultPrevented, zoom: elements.zoomOutput.textContent});
      }
      const button = element("BUTTON");
      elements.stage.dispatch("pointerdown", {
        pointerId: 20, clientX: 20, clientY: 20, target: button,
      });
      elements.stage.dispatch("pointerup", {
        pointerId: 20, clientX: 20, clientY: 20, target: button,
      });
      elements.stage.dispatch("pointerdown", {
        pointerId: 21, clientX: 30, clientY: 30, ctrlKey: true,
      });
      elements.stage.dispatch("pointerup", {
        pointerId: 21, clientX: 30, clientY: 30, ctrlKey: true,
      });
      process.stdout.write(JSON.stringify({
        keyOutcomes, wheelOutcomes, requests: requests.length,
        inspected, selected: elements.selection.dataset,
      }));
    """)

    assert result == {
        "keyOutcomes": [{"prevented": False, "zoom": "19%"}] * 8,
        "wheelOutcomes": [{"prevented": False, "zoom": "19%"}] * 4,
        "requests": 0,
        "inspected": [],
        "selected": {},
    }


def test_viewport_click_inspects_once_and_records_selection_coordinates():
    """Catches clicks becoming pans or inspection state missing its generation contract."""
    result = run_viewport("""\
      const inspected = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async () => ({id: "frame"}),
        makeObjectUrl: () => "blob:frame",
        revokeObjectUrl: () => {},
        onInspect: (x, y) => inspected.push({x, y}),
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      const down = elements.stage.dispatch("pointerdown", {
        pointerId: 9, clientX: 315.5, clientY: 93.75,
      });
      const up = elements.stage.dispatch("pointerup", {
        pointerId: 9, clientX: 318.5, clientY: 95.75,
      });
      await up.result;
      process.stdout.write(JSON.stringify({
        inspected,
        dataset: elements.selection.dataset,
        cursor: {hidden: elements.pixelCursor.hidden, ...elements.pixelCursor.style},
        capturedAfterDown: down.currentTarget.capturedPointer,
        capturedAfterUp: up.currentTarget.capturedPointer,
      }));
    """)

    assert result["inspected"] == [{"x": 1016, "y": 510}]
    assert result["dataset"] == {"x": "1016", "y": "510"}
    assert result["cursor"] == {"hidden": False, "left": "318.59375px", "top": "95.71875px"}
    assert result["capturedAfterUp"] is None


def test_viewport_click_in_letterbox_margin_does_not_inspect_edge_pixel():
    """Catches empty stage margins clamping into false scientific selections."""
    result = run_viewport("""\
      const inspected = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async () => ({id: "frame"}),
        makeObjectUrl: () => "blob:frame",
        revokeObjectUrl: () => {},
        onInspect: (x, y) => inspected.push({x, y}),
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      elements.stage.dispatch("pointerdown", {pointerId: 13, clientX: 64, clientY: 300});
      const up = elements.stage.dispatch("pointerup", {
        pointerId: 13, clientX: 64, clientY: 300,
      });
      await up.result;
      process.stdout.write(JSON.stringify({inspected, selected: elements.selection.dataset}));
    """)

    assert result == {"inspected": [], "selected": {}}


def test_viewport_plain_drag_above_threshold_pans_without_inspection():
    """Catches pan drags firing raw-pixel requests or waiting until pointer-up."""
    result = run_viewport("""\
      const inspected = [];
      const requests = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async (url) => { requests.push(url); return {id: requests.length}; },
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: () => {},
        onInspect: (x, y) => inspected.push({x, y}),
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      await controller.oneToOne();
      requests.length = 0;
      elements.stage.dispatch("pointerdown", {pointerId: 4, clientX: 500, clientY: 400});
      const move = elements.stage.dispatch("pointermove", {
        pointerId: 4, clientX: 550, clientY: 450, buttons: 1,
      });
      await move.result;
      const up = elements.stage.dispatch("pointerup", {pointerId: 4, clientX: 550, clientY: 450});
      await up.result;
      const query = Object.fromEntries(new URL(`http://test${requests.at(-1)}`).searchParams);
      process.stdout.write(JSON.stringify({
        inspected,
        crop: {x: query.x, y: query.y},
        mode: elements.stage.dataset.viewportMode,
        captured: elements.stage.capturedPointer,
      }));
    """)

    assert result == {
        "inspected": [],
        "crop": {"x": "1486", "y": "1614"},
        "mode": "inspect",
        "captured": None,
    }


def test_viewport_coalesces_rapid_pan_and_wheel_renders():
    """Catches high-frequency pointer input flooding crop requests."""
    result = run_viewport("""\
      const requests = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async (url) => { requests.push(url); return {id: requests.length}; },
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: () => {},
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      await controller.oneToOne();
      requests.length = 0;
      elements.stage.dispatch("pointerdown", {pointerId: 40, clientX: 500, clientY: 400});
      const moves = [510, 520, 530].map((clientX) => elements.stage.dispatch(
        "pointermove",
        {pointerId: 40, clientX, clientY: 400, buttons: 1},
      ));
      await Promise.all(moves.map((event) => event.result));
      elements.stage.dispatch("pointerup", {pointerId: 40, clientX: 530, clientY: 400});
      const panRequests = requests.length;
      requests.length = 0;
      elements.stage.focus();
      const wheels = [1, 2, 3].map(() => elements.stage.dispatch("wheel", {
        clientX: 512, clientY: 384, deltaY: -100,
      }));
      await Promise.all(wheels.map((event) => event.result));
      process.stdout.write(JSON.stringify({
        panRequests,
        wheelRequests: requests.length,
        wheelPrevented: wheels.every((event) => event.defaultPrevented),
      }));
    """)

    assert result == {"panRequests": 1, "wheelRequests": 1, "wheelPrevented": True}


def test_viewport_refits_and_disconnects_when_the_stage_resizes():
    """Catches responsive layout changes leaving stale crop geometry or observers."""
    result = run_viewport("""\
      let resizeCallback;
      let disconnected = false;
      globalThis.ResizeObserver = class {
        constructor(callback) { resizeCallback = callback; }
        observe() {}
        disconnect() { disconnected = true; }
      };
      const requests = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async (url) => { requests.push(url); return {id: requests.length}; },
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: () => {},
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      elements.stage.clientWidth = 512;
      elements.stage.clientHeight = 384;
      await resizeCallback();
      const query = Object.fromEntries(new URL(`http://test${requests.at(-1)}`).searchParams);
      controller.destroy();
      process.stdout.write(JSON.stringify({
        zoom: elements.zoomOutput.textContent,
        crop: {width: query.width, height: query.height, maxWidth: query.maxWidth},
        disconnected,
      }));
    """)

    assert result == {
        "zoom": "9%",
        "crop": {"width": "4096", "height": "4096", "maxWidth": "384"},
        "disconnected": True,
    }


def test_viewport_resize_bounds_custom_scale_for_crop_zoom_and_marker():
    """Catches rendering at the new fit scale while UI transforms retain the old scale."""
    result = run_viewport("""\
      let resizeCallback;
      globalThis.ResizeObserver = class {
        constructor(callback) { resizeCallback = callback; }
        observe() {}
        disconnect() {}
      };
      elements.stage.clientWidth = 512;
      elements.stage.clientHeight = 384;
      const requests = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async (url) => { requests.push(url); return {id: requests.length}; },
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: () => {},
      });
      controller.setGeneration(7, {width: 512, height: 384});
      await controller.oneToOne();
      elements.stage.dispatch("pointerdown", {
        pointerId: 50, clientX: 100, clientY: 100,
      });
      await elements.stage.dispatch("pointerup", {
        pointerId: 50, clientX: 100, clientY: 100,
      }).result;
      requests.length = 0;
      elements.stage.clientWidth = 1024;
      elements.stage.clientHeight = 768;
      await resizeCallback();
      const query = Object.fromEntries(new URL(`http://test${requests[0]}`).searchParams);
      const layer = elements.layerHost.children[0];
      process.stdout.write(JSON.stringify({
        zoom: elements.zoomOutput.textContent,
        crop: {
          width: query.width,
          height: query.height,
          maxWidth: query.maxWidth,
          maxHeight: query.maxHeight,
        },
        layer: {
          left: layer.style.left,
          top: layer.style.top,
          width: layer.style.width,
          height: layer.style.height,
        },
        marker: {
          left: elements.pixelCursor.style.left,
          top: elements.pixelCursor.style.top,
        },
      }));
    """)

    assert result == {
        "zoom": "200%",
        "crop": {
            "width": "512",
            "height": "384",
            "maxWidth": "1024",
            "maxHeight": "768",
        },
        "layer": {
            "left": "0px",
            "top": "0px",
            "width": "1024px",
            "height": "768px",
        },
        "marker": {"left": "201px", "top": "201px"},
    }


def test_viewport_space_drag_forces_pan_and_only_prevents_scroll_when_active():
    """Catches global Space interception or forced-pan gestures inspecting pixels."""
    result = run_viewport("""\
      const inspected = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async () => ({id: "frame"}),
        makeObjectUrl: () => "blob:frame",
        revokeObjectUrl: () => {},
        onInspect: (x, y) => inspected.push({x, y}),
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      await controller.oneToOne();
      globalThis.document.activeElement = null;
      const inactiveSpace = elements.stage.dispatch("keydown", {key: " ", code: "Space"});
      elements.stage.focus();
      const activeSpace = elements.stage.dispatch("keydown", {key: " ", code: "Space"});
      elements.stage.dispatch("pointerdown", {pointerId: 5, clientX: 500, clientY: 400});
      const move = elements.stage.dispatch("pointermove", {
        pointerId: 5, clientX: 510, clientY: 400, buttons: 1,
      });
      await move.result;
      const up = elements.stage.dispatch("pointerup", {pointerId: 5, clientX: 510, clientY: 400});
      await up.result;
      const releaseSpace = elements.stage.dispatch("keyup", {
        key: " ", code: "Space", ctrlKey: true,
      });
      process.stdout.write(JSON.stringify({
        inactivePrevented: inactiveSpace.defaultPrevented,
        activePrevented: activeSpace.defaultPrevented,
        releasePrevented: releaseSpace.defaultPrevented,
        inspected,
        mode: elements.stage.dataset.viewportMode,
      }));
    """)

    assert result == {
        "inactivePrevented": False,
        "activePrevented": True,
        "releasePrevented": False,
        "inspected": [],
        "mode": "inspect",
    }


def test_viewport_shift_drag_in_every_direction_fits_the_same_view_only_crop():
    """Catches direction-dependent box selection or a mode that changes mid-gesture."""
    result = run_viewport("""\
      const requests = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async (url) => { requests.push(url); return {id: requests.length}; },
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: () => {},
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      const corners = [
        [{x: 256, y: 192}, {x: 768, y: 576}],
        [{x: 768, y: 192}, {x: 256, y: 576}],
        [{x: 256, y: 576}, {x: 768, y: 192}],
        [{x: 768, y: 576}, {x: 256, y: 192}],
      ];
      const crops = [];
      let visibleLabel = "";
      for (const [start, end] of corners) {
        await controller.fit();
        requests.length = 0;
        elements.stage.dispatch("pointerdown", {pointerId: 6, ...start, clientX: start.x, clientY: start.y, shiftKey: true});
        const move = elements.stage.dispatch("pointermove", {
          pointerId: 6, clientX: end.x, clientY: end.y, buttons: 1, shiftKey: false,
        });
        await move.result;
        visibleLabel = elements.selectionLabel.textContent;
        const up = elements.stage.dispatch("pointerup", {
          pointerId: 6, clientX: end.x, clientY: end.y, shiftKey: false,
        });
        await up.result;
        const parsed = requests.map((url) => Object.fromEntries(new URL(`http://test${url}`).searchParams));
        crops.push({
          x: Math.min(...parsed.map((query) => Number(query.x))),
          y: Math.min(...parsed.map((query) => Number(query.y))),
          right: Math.max(...parsed.map((query) => Number(query.x) + Number(query.width))),
          bottom: Math.max(...parsed.map((query) => Number(query.y) + Number(query.height))),
        });
      }
      process.stdout.write(JSON.stringify({
        crops,
        labelIsViewOnly: visibleLabel.startsWith("View only") &&
          visibleLabel.includes("682") && visibleLabel.includes("1024"),
        hidden: elements.selection.hidden,
      }));
    """)

    assert result["crops"] == [result["crops"][0]] * 4
    assert result["crops"][0] == {"x": 682, "y": 1023, "right": 3414, "bottom": 3073}
    assert result["labelIsViewOnly"] is True
    assert result["hidden"] is True


def test_viewport_box_zoom_toolbar_locks_view_only_selection_mode():
    """Catches the visible Box zoom control failing to enable its pointer mode."""
    result = run_viewport("""\
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async () => ({id: "frame"}),
        makeObjectUrl: () => "blob:frame",
        revokeObjectUrl: () => {},
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      elements.boxZoom.dispatch("click");
      const pressed = elements.boxZoom.attributes["aria-pressed"];
      elements.stage.dispatch("pointerdown", {
        pointerId: 12, clientX: 200, clientY: 200,
      });
      elements.stage.dispatch("pointermove", {
        pointerId: 12, clientX: 600, clientY: 500, buttons: 1,
      });
      const labelWasVisible = !elements.selectionLabel.hidden &&
        elements.selectionLabel.textContent.startsWith("View only");
      const up = elements.stage.dispatch("pointerup", {
        pointerId: 12, clientX: 600, clientY: 500,
      });
      await up.result;
      elements.boxZoom.dispatch("click");
      process.stdout.write(JSON.stringify({
        pressed,
        released: elements.boxZoom.attributes["aria-pressed"],
        labelWasVisible,
        zoom: elements.zoomOutput.textContent,
        mode: elements.stage.dataset.viewportMode,
      }));
    """)

    assert result == {
        "pressed": "true",
        "released": "false",
        "labelWasVisible": True,
        "zoom": "48%",
        "mode": "inspect",
    }


def test_viewport_escape_cancels_shift_drag_without_changing_the_view():
    """Catches cancelled selections applying a crop when pointer-up arrives later."""
    result = run_viewport("""\
      const requests = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async (url) => { requests.push(url); return {id: requests.length}; },
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: () => {},
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      elements.stage.dispatch("pointerdown", {
        pointerId: 8, clientX: 200, clientY: 200, shiftKey: true,
      });
      elements.stage.dispatch("pointermove", {
        pointerId: 8, clientX: 600, clientY: 500, buttons: 1,
      });
      const escape = elements.stage.dispatch("keydown", {key: "Escape", code: "Escape"});
      const up = elements.stage.dispatch("pointerup", {pointerId: 8, clientX: 600, clientY: 500});
      await up.result;
      process.stdout.write(JSON.stringify({
        prevented: escape.defaultPrevented,
        requests: requests.length,
        zoom: elements.zoomOutput.textContent,
        hidden: elements.selection.hidden,
      }));
    """)

    assert result == {"prevented": True, "requests": 0, "zoom": "19%", "hidden": True}


def test_viewport_arrow_shortcuts_move_inspection_by_one_and_ten_pixels():
    """Catches keyboard inspection drift or Shift applying a zoom mode instead."""
    result = run_viewport("""\
      const inspected = [];
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async () => ({id: "frame"}),
        makeObjectUrl: () => "blob:frame",
        revokeObjectUrl: () => {},
        onInspect: (x, y) => inspected.push({x, y}),
      });
      controller.setGeneration(7, {width: 4096, height: 4096});
      elements.stage.dispatch("pointerdown", {pointerId: 3, clientX: 146.75, clientY: 18.75});
      const click = elements.stage.dispatch("pointerup", {pointerId: 3, clientX: 146.75, clientY: 18.75});
      await click.result;
      const right = elements.stage.dispatch("keydown", {key: "ArrowRight", code: "ArrowRight"});
      await right.result;
      const down = elements.stage.dispatch("keydown", {
        key: "ArrowDown", code: "ArrowDown", shiftKey: true,
      });
      await down.result;
      process.stdout.write(JSON.stringify({
        inspected,
        dataset: elements.selection.dataset,
        prevented: [right.defaultPrevented, down.defaultPrevented],
      }));
    """)

    assert result == {
        "inspected": [{"x": 100, "y": 100}, {"x": 101, "y": 100}, {"x": 101, "y": 110}],
        "dataset": {"x": "101", "y": "110"},
        "prevented": [True, True],
    }


def test_viewport_retry_button_restarts_a_failed_render():
    """Catches Retry being present but disconnected from the failed request lifecycle."""
    result = run_viewport("""\
      let attempts = 0;
      const controller = viewport.createImageViewport({
        elements,
        fetchBlob: async () => {
          attempts += 1;
          if (attempts === 1) throw new Error("temporary failure");
          return {id: "recovered"};
        },
        makeObjectUrl: (blob) => `blob:${blob.id}`,
        revokeObjectUrl: () => {},
        onError: () => {},
      });
      controller.setGeneration(7, {width: 1024, height: 768});
      await controller.refresh();
      const visibleAfterFailure = !elements.retry.hidden;
      const retry = elements.retry.dispatch("click");
      await retry.result;
      process.stdout.write(JSON.stringify({
        attempts,
        visibleAfterFailure,
        hiddenAfterRecovery: elements.retry.hidden,
        layers: elements.layerHost.children.map((layer) => layer.src),
      }));
    """)

    assert result == {
        "attempts": 2,
        "visibleAfterFailure": True,
        "hiddenAfterRecovery": True,
        "layers": ["blob:recovered"],
    }


def test_image_viewer_composes_crops_stretch_and_raw_pixel_inspection():
    """Catches a legacy full-image path or display-derived pixel inspection."""
    result = run_viewer("""\
      const requests = [];
      globalThis.fetch = async (url, options = {}) => {
        requests.push({url, signal: options.signal});
        if (url.startsWith("/web/v1/viewport.png")) {
          return {ok: true, blob: async () => ({id: requests.length})};
        }
        if (url.startsWith("/web/v1/statistics")) {
          return {ok: true, json: async () => ({
            generation: 7, minimum: 0, maximum: 4095, mean: 20,
            standardDeviation: 3, fwhm: {x: null, y: null}, histogram: [1, 2],
          })};
        }
        if (url.startsWith("/web/v1/pixel")) {
          return {ok: true, json: async () => ({generation: 7, x: 100, y: 100, value: 1234})};
        }
        throw new Error(`unexpected request ${url}`);
      };
      viewer.bindImageViewer();
      viewer.setGeneration(7, {width: 1024, height: 768, maxADU: 4095});
      await new Promise((resolve) => setTimeout(resolve, 10));
      elements["image-stage"].dispatch("pointerdown", {
        pointerId: 2, clientX: 100, clientY: 100,
      });
      const inspected = elements["image-stage"].dispatch("pointerup", {
        pointerId: 2, clientX: 100, clientY: 100,
      });
      await inspected.result;
      elements["black-point"].value = "100";
      elements["black-point"].dispatch("input");
      await new Promise((resolve) => setTimeout(resolve, 140));
      const urls = requests.map((request) => request.url);
      process.stdout.write(JSON.stringify({
        cropUrls: urls.filter((url) => url.startsWith("/web/v1/viewport.png")),
        pixelUrl: urls.find((url) => url.startsWith("/web/v1/pixel")),
        pixelReadoutMatches:
          elements["pixel-readout"].textContent.startsWith("Pixel 100, 100") &&
          elements["pixel-readout"].textContent.endsWith("1234 ADU"),
        selected: elements["viewport-selection"].dataset,
        layers: elements["viewport-layer-host"].children.length,
      }));
    """)

    assert result["cropUrls"][0].startswith(
        "/web/v1/viewport.png?generation=7&x=0&y=0&width=1024&height=768"
    )
    assert "black=100" in result["cropUrls"][-1]
    assert result["pixelUrl"] == "/web/v1/pixel?generation=7&x=100&y=100"
    assert result["pixelReadoutMatches"] is True
    assert result["selected"] == {"x": "100", "y": "100"}
    assert result["layers"] == 1


def test_destroy_image_viewer_aborts_pending_crops_and_removes_gestures():
    """Catches app-lifetime teardown leaving requests or stage listeners alive."""
    result = run_viewer("""\
      const requests = [];
      globalThis.fetch = (url, options = {}) => {
        requests.push({url, signal: options.signal});
        if (url.startsWith("/web/v1/statistics")) {
          return Promise.resolve({ok: true, json: async () => ({
            generation: 7, minimum: 0, maximum: 1, mean: 0,
            standardDeviation: 0, fwhm: {x: null, y: null}, histogram: [0],
          })});
        }
        return new Promise((resolve, reject) => {
          options.signal?.addEventListener("abort", () => {
            const error = new Error("aborted");
            error.name = "AbortError";
            reject(error);
          });
        });
      };
      viewer.bindImageViewer();
      viewer.setGeneration(7, {width: 1024, height: 768, maxADU: 4095});
      await new Promise((resolve) => setTimeout(resolve, 10));
      const crop = requests.find((request) => request.url.startsWith("/web/v1/viewport.png"));
      viewer.destroyImageViewer();
      const countAtDestroy = requests.length;
      elements["image-stage"].focus();
      const wheel = elements["image-stage"].dispatch("wheel", {
        clientX: 300, clientY: 200, deltaY: -100,
      });
      await new Promise((resolve) => setTimeout(resolve, 10));
      process.stdout.write(JSON.stringify({
        cropAborted: crop.signal.aborted,
        countAtDestroy,
        countAfterWheel: requests.length,
        wheelPrevented: wheel.defaultPrevented,
      }));
    """)

    assert result["cropAborted"] is True
    assert result["countAfterWheel"] == result["countAtDestroy"]
    assert result["wheelPrevented"] is False


def test_image_viewer_retry_clears_error_after_a_successful_crop():
    """Catches Retry loading new layers while leaving the error state over them."""
    result = run_viewer("""\
      let viewportAttempts = 0;
      globalThis.fetch = async (url) => {
        if (url.startsWith("/web/v1/viewport.png")) {
          viewportAttempts += 1;
          if (viewportAttempts === 1) {
            return {
              ok: false, status: 503, statusText: "Unavailable",
              json: async () => ({error: "temporary render failure"}),
            };
          }
          return {ok: true, blob: async () => ({id: "recovered"})};
        }
        if (url.startsWith("/web/v1/statistics")) {
          return {ok: true, json: async () => ({
            generation: 7, minimum: 0, maximum: 1, mean: 0,
            standardDeviation: 0, fwhm: {x: null, y: null}, histogram: [0],
          })};
        }
        throw new Error(`unexpected request ${url}`);
      };
      viewer.bindImageViewer();
      viewer.setGeneration(7, {width: 1024, height: 768, maxADU: 4095});
      await new Promise((resolve) => setTimeout(resolve, 10));
      const errorVisible = !elements["image-error"].hidden;
      const retry = elements["viewport-retry"].dispatch("click");
      await retry.result;
      process.stdout.write(JSON.stringify({
        viewportAttempts,
        errorVisible,
        errorHiddenAfterRetry: elements["image-error"].hidden,
        retryHidden: elements["viewport-retry"].hidden,
        layers: elements["viewport-layer-host"].children.map((layer) => layer.src),
      }));
    """)

    assert result == {
        "viewportAttempts": 2,
        "errorVisible": True,
        "errorHiddenAfterRetry": True,
        "retryHidden": True,
        "layers": ["blob:recovered"],
    }


def test_image_viewer_shape_change_aborts_old_pixel_and_clears_stale_adu():
    """Catches old-generation ADU work surviving an invalidating shape change."""
    result = run_viewer("""\
      let pixelAttempt = 0;
      let pendingPixelSignal;
      globalThis.fetch = async (url, options = {}) => {
        if (url.startsWith("/web/v1/viewport.png")) {
          return {ok: true, blob: async () => ({id: url})};
        }
        if (url.startsWith("/web/v1/statistics")) {
          const generation = Number(new URL(`http://test${url}`).searchParams.get("generation"));
          return {ok: true, json: async () => ({
            generation, minimum: 0, maximum: 1, mean: 0,
            standardDeviation: 0, fwhm: {x: null, y: null}, histogram: [0],
          })};
        }
        if (url.startsWith("/web/v1/pixel")) {
          pixelAttempt += 1;
          if (pixelAttempt === 1) {
            return {ok: true, json: async () => ({generation: 7, x: 900, y: 700, value: 1234})};
          }
          pendingPixelSignal = options.signal;
          return new Promise((resolve, reject) => {
            options.signal.addEventListener("abort", () => {
              const error = new Error("aborted");
              error.name = "AbortError";
              reject(error);
            });
          });
        }
        throw new Error(`unexpected request ${url}`);
      };
      viewer.bindImageViewer();
      viewer.setGeneration(7, {width: 1024, height: 768, maxADU: 4095});
      await new Promise((resolve) => setTimeout(resolve, 10));
      elements["image-stage"].dispatch("pointerdown", {
        pointerId: 30, clientX: 900, clientY: 700,
      });
      const first = elements["image-stage"].dispatch("pointerup", {
        pointerId: 30, clientX: 900, clientY: 700,
      });
      await first.result;
      elements["image-stage"].dispatch("pointerdown", {
        pointerId: 31, clientX: 901, clientY: 701,
      });
      elements["image-stage"].dispatch("pointerup", {
        pointerId: 31, clientX: 901, clientY: 701,
      });
      await new Promise((resolve) => setTimeout(resolve, 0));
      viewer.setGeneration(8, {width: 512, height: 512, maxADU: 4095});
      await new Promise((resolve) => setTimeout(resolve, 10));
      process.stdout.write(JSON.stringify({
        pendingPixelAborted: pendingPixelSignal.aborted,
        readoutCleared: elements["pixel-readout"].textContent.startsWith("Pixel") &&
          !elements["pixel-readout"].textContent.includes("ADU"),
        selected: elements["viewport-selection"].dataset,
      }));
    """)

    assert result == {
        "pendingPixelAborted": True,
        "readoutCleared": True,
        "selected": {},
    }


def test_image_viewer_same_shape_generation_reinspects_selected_raw_pixel():
    """Catches retained coordinates showing an old generation's ADU value."""
    result = run_viewer("""\
      let oldPixelSignal;
      const pixelUrls = [];
      globalThis.fetch = async (url, options = {}) => {
        if (url.startsWith("/web/v1/viewport.png")) {
          return {ok: true, blob: async () => ({id: url})};
        }
        if (url.startsWith("/web/v1/statistics")) {
          const generation = Number(new URL(`http://test${url}`).searchParams.get("generation"));
          return {ok: true, json: async () => ({
            generation, minimum: 0, maximum: 1, mean: 0,
            standardDeviation: 0, fwhm: {x: null, y: null}, histogram: [0],
          })};
        }
        if (url.startsWith("/web/v1/pixel")) {
          pixelUrls.push(url);
          const generation = Number(new URL(`http://test${url}`).searchParams.get("generation"));
          if (generation === 8) {
            return {ok: true, json: async () => ({generation, x: 100, y: 100, value: 2222})};
          }
          oldPixelSignal = options.signal;
          return new Promise((resolve, reject) => {
            options.signal.addEventListener("abort", () => {
              const error = new Error("aborted");
              error.name = "AbortError";
              reject(error);
            });
          });
        }
        throw new Error(`unexpected request ${url}`);
      };
      viewer.bindImageViewer();
      viewer.setGeneration(7, {width: 1024, height: 768, maxADU: 4095});
      await new Promise((resolve) => setTimeout(resolve, 10));
      elements["image-stage"].dispatch("pointerdown", {
        pointerId: 32, clientX: 100, clientY: 100,
      });
      elements["image-stage"].dispatch("pointerup", {
        pointerId: 32, clientX: 100, clientY: 100,
      });
      await new Promise((resolve) => setTimeout(resolve, 0));
      viewer.setGeneration(8, {width: 1024, height: 768, maxADU: 4095});
      await new Promise((resolve) => setTimeout(resolve, 10));
      process.stdout.write(JSON.stringify({
        oldPixelAborted: oldPixelSignal.aborted,
        selected: elements["viewport-selection"].dataset,
        latestPixelUrl: pixelUrls.at(-1),
        readoutMatches:
          elements["pixel-readout"].textContent.startsWith("Pixel 100, 100") &&
          elements["pixel-readout"].textContent.endsWith("2222 ADU"),
      }));
    """)

    assert result == {
        "oldPixelAborted": True,
        "selected": {"x": "100", "y": "100"},
        "latestPixelUrl": "/web/v1/pixel?generation=8&x=100&y=100",
        "readoutMatches": True,
    }


def test_app_teardown_destroys_viewer_before_closing_event_stream():
    """Catches unload closing state events while viewport work remains active."""
    script = f"""
      const app = await import({json.dumps(APP_MODULE)});
      const order = [];
      app.teardownApp({{
        destroyViewer: () => order.push("viewer"),
        clearRefresh: () => order.push("interval"),
        eventSource: {{close: () => order.push("stream")}},
      }});
      process.stdout.write(JSON.stringify(order));
    """
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == ["viewer", "interval", "stream"]
