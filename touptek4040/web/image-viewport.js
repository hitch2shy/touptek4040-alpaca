import { getBlob } from "./api.js";
import { generationUrl } from "./generation-contract.js";
import {
  MAX_SCALE,
  displayRect,
  fitView,
  panBy,
  planCropSegments,
  screenToImage,
  selectionToImageRect,
  visibleRect,
  zoomAt,
} from "./viewport-math.js";

const DEFAULT_STRETCH = { black: 0, white: 4095, gamma: 1 };
const DRAG_THRESHOLD = 4;
const ZOOM_FACTOR = 1.25;
const ZOOM_STEPS = Math.log2(ZOOM_FACTOR);

function viewportSize(stage) {
  const bounds = stage.getBoundingClientRect();
  const width = bounds.width || stage.clientWidth;
  const height = bounds.height || stage.clientHeight;
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    throw new TypeError("viewport stage must have positive dimensions");
  }
  return { width, height };
}

function sameShape(first, second) {
  return first?.width === second?.width && first?.height === second?.height;
}

function isAbort(error) {
  return error?.name === "AbortError";
}

function localPoint(stage, event) {
  const bounds = stage.getBoundingClientRect();
  return { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
}

function distance(first, second) {
  return Math.hypot(second.x - first.x, second.y - first.y);
}

function clampPixel(value, maximum) {
  return Math.min(maximum - 1, Math.max(0, Math.floor(value)));
}

function hasForbiddenModifier(event) {
  return event.ctrlKey || event.altKey || event.metaKey;
}

function isControlTarget(target) {
  if (!target) return false;
  if (target.isContentEditable) return true;
  if (["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(target.tagName)) return true;
  return Boolean(target.closest?.(
    "input, select, textarea, button, [contenteditable]:not([contenteditable='false'])",
  ));
}

export function createImageViewport({
  elements,
  fetchBlob = getBlob,
  makeObjectUrl = (blob) => URL.createObjectURL(blob),
  revokeObjectUrl = (url) => URL.revokeObjectURL(url),
  onInspect = () => {},
  onGenerationConflict = () => {},
  onError = () => {},
  onRendered = () => {},
} = {}) {
  if (!elements?.stage || !elements.layerHost || !elements.retry) {
    throw new TypeError("viewport elements are required");
  }

  let activeGeneration = null;
  let dimensions = null;
  let stretch = DEFAULT_STRETCH;
  let view = null;
  let viewMode = "fit";
  let renderSequence = 0;
  let destroyed = false;
  let ownedUrls = [];
  let requestControllers = new Set();
  let gesture = null;
  let boxMode = false;
  let spaceHeld = false;
  let scheduledRenderTimer = null;
  let scheduledRenderPromise = null;
  let resolveScheduledRender = null;
  let resizeObserver = null;
  const listeners = [];

  function listen(element, name, handler, options) {
    if (!element) return;
    element.addEventListener(name, handler, options);
    listeners.push(() => element.removeEventListener(name, handler, options));
  }

  function cancelRequests() {
    requestControllers.forEach((controller) => controller.abort());
    requestControllers = new Set();
  }

  function cancelScheduledRender() {
    clearTimeout(scheduledRenderTimer);
    scheduledRenderTimer = null;
    resolveScheduledRender?.();
    resolveScheduledRender = null;
    scheduledRenderPromise = null;
  }

  function scheduleRefresh() {
    if (destroyed) return Promise.resolve();
    if (!scheduledRenderPromise) {
      scheduledRenderPromise = new Promise((resolve) => {
        resolveScheduledRender = resolve;
      });
    }
    clearTimeout(scheduledRenderTimer);
    scheduledRenderTimer = setTimeout(async () => {
      const resolve = resolveScheduledRender;
      scheduledRenderTimer = null;
      scheduledRenderPromise = null;
      resolveScheduledRender = null;
      await refresh();
      resolve?.();
    }, 16);
    return scheduledRenderPromise;
  }

  function revoke(urls) {
    urls.forEach((url) => revokeObjectUrl(url));
  }

  function clearLayers() {
    revoke(ownedUrls);
    ownedUrls = [];
    elements.layerHost.replaceChildren();
  }

  function clearInspection() {
    if (elements.selection) {
      delete elements.selection.dataset.x;
      delete elements.selection.dataset.y;
      elements.selection.hidden = true;
    }
    if (elements.pixelCursor) elements.pixelCursor.hidden = true;
  }

  function inspectionCoordinate() {
    const x = Number(elements.selection?.dataset?.x);
    const y = Number(elements.selection?.dataset?.y);
    return Number.isInteger(x) && Number.isInteger(y) ? { x, y } : null;
  }

  function positionInspection() {
    const selected = inspectionCoordinate();
    if (!selected || !view || !dimensions || !elements.pixelCursor) return;
    const size = viewportSize(elements.stage);
    const visible = visibleRect(
      view,
      dimensions.width,
      dimensions.height,
      size.width,
      size.height,
    );
    const display = displayRect(
      view,
      dimensions.width,
      dimensions.height,
      size.width,
      size.height,
    );
    const left = display.left + (selected.x + 0.5 - visible.x) * view.scale;
    const top = display.top + (selected.y + 0.5 - visible.y) * view.scale;
    elements.pixelCursor.style.left = `${left}px`;
    elements.pixelCursor.style.top = `${top}px`;
    elements.pixelCursor.hidden = left < 0 || left > size.width || top < 0 || top > size.height;
  }

  function clearOutOfBoundsInspection() {
    const selected = inspectionCoordinate();
    const inBounds = selected && selected.x >= 0 && selected.y >= 0 &&
      selected.x < dimensions.width && selected.y < dimensions.height;
    if (!inBounds) clearInspection();
    else positionInspection();
  }

  function setZoomOutput() {
    if (elements.zoomOutput) {
      elements.zoomOutput.textContent = view ? `${Math.round(view.scale * 100)}%` : "";
    }
    positionInspection();
  }

  function resetToFit() {
    if (!dimensions) {
      view = null;
      setZoomOutput();
      return;
    }
    const size = viewportSize(elements.stage);
    view = fitView(dimensions.width, dimensions.height, size.width, size.height);
    viewMode = "fit";
    setZoomOutput();
  }

  function normalizedView(nextView) {
    const size = viewportSize(elements.stage);
    const visible = visibleRect(
      nextView,
      dimensions.width,
      dimensions.height,
      size.width,
      size.height,
    );
    return {
      scale: Math.min(MAX_SCALE, nextView.scale),
      centerX: visible.x + visible.width / 2,
      centerY: visible.y + visible.height / 2,
    };
  }

  function segmentUrl(segment) {
    const maxWidth = Math.max(1, Math.min(2048, Math.ceil(segment.displayWidth)));
    const maxHeight = Math.max(1, Math.min(2048, Math.ceil(segment.displayHeight)));
    return generationUrl("/web/v1/viewport.png", activeGeneration, {
      x: segment.x,
      y: segment.y,
      width: segment.width,
      height: segment.height,
      black: stretch.black,
      white: stretch.white,
      gamma: stretch.gamma,
      maxWidth,
      maxHeight,
    });
  }

  function makeLayer(segment, url) {
    const layer = document.createElement("img");
    layer.src = url;
    layer.alt = "";
    layer.className = "viewport-layer";
    layer.dataset.pixelated = String(view.scale >= 1);
    layer.style.left = `${segment.left}px`;
    layer.style.top = `${segment.top}px`;
    layer.style.width = `${segment.displayWidth}px`;
    layer.style.height = `${segment.displayHeight}px`;
    return layer;
  }

  async function refresh() {
    if (destroyed || activeGeneration == null || !dimensions || !view) return;

    const sequence = ++renderSequence;
    cancelRequests();
    elements.retry.hidden = true;
    let unpublishedUrls = [];
    try {
      const size = viewportSize(elements.stage);
      const segments = planCropSegments(
        view,
        dimensions.width,
        dimensions.height,
        size.width,
        size.height,
      );
      const results = await Promise.all(segments.map(async (segment) => {
        const controller = new AbortController();
        requestControllers.add(controller);
        try {
          const blob = await fetchBlob(segmentUrl(segment), controller.signal);
          return { segment, blob };
        } finally {
          requestControllers.delete(controller);
        }
      }));

      if (destroyed || sequence !== renderSequence) return;
      const layers = results.map(({ segment, blob }) => {
        const url = makeObjectUrl(blob);
        unpublishedUrls.push(url);
        return makeLayer(segment, url);
      });
      if (destroyed || sequence !== renderSequence) return;

      const replacedUrls = ownedUrls;
      ownedUrls = unpublishedUrls;
      unpublishedUrls = [];
      elements.layerHost.replaceChildren(...layers);
      revoke(replacedUrls);
      onRendered();
    } catch (error) {
      if (destroyed || sequence !== renderSequence || isAbort(error)) return;
      cancelRequests();
      if (error?.status === 409) {
        onGenerationConflict(error);
        return;
      }
      elements.retry.hidden = false;
      onError(error);
    } finally {
      revoke(unpublishedUrls);
    }
  }

  function cancelGesture() {
    if (gesture && elements.stage.hasPointerCapture?.(gesture.pointerId)) {
      elements.stage.releasePointerCapture(gesture.pointerId);
    }
    gesture = null;
    if (elements.selection) elements.selection.hidden = true;
    if (elements.selectionLabel) elements.selectionLabel.hidden = true;
    syncCursorMode();
  }

  function setGeneration(generation, nextDimensions = null) {
    if (destroyed) return;
    const generationChanged = generation !== activeGeneration;
    const shapeChanged = !sameShape(dimensions, nextDimensions);
    if (generationChanged || shapeChanged) {
      ++renderSequence;
      cancelScheduledRender();
      cancelRequests();
    }
    if (shapeChanged) cancelGesture();

    activeGeneration = generation;
    dimensions = nextDimensions;
    if (generation == null) {
      clearLayers();
      view = null;
      clearInspection();
      if (elements.selectionLabel) elements.selectionLabel.hidden = true;
      elements.retry.hidden = true;
      setZoomOutput();
      return;
    }

    if (!view || shapeChanged) {
      resetToFit();
      if (shapeChanged) clearOutOfBoundsInspection();
    } else {
      setZoomOutput();
    }
  }

  function setStretch(nextStretch) {
    stretch = { ...nextStretch };
  }

  function fit() {
    if (destroyed || !dimensions) return;
    resetToFit();
    return refresh();
  }

  function oneToOne() {
    if (destroyed || !dimensions) return;
    const size = viewportSize(elements.stage);
    const fitted = fitView(dimensions.width, dimensions.height, size.width, size.height);
    view = normalizedView({
      scale: Math.min(MAX_SCALE, Math.max(fitted.scale, 1)),
      centerX: dimensions.width / 2,
      centerY: dimensions.height / 2,
    });
    viewMode = "custom";
    setZoomOutput();
    return refresh();
  }

  function zoomAtPoint(factor, point, scheduled = false) {
    if (destroyed || !view || !dimensions) return;
    const size = viewportSize(elements.stage);
    view = zoomAt(
      view,
      Math.log2(factor),
      point.x,
      point.y,
      dimensions.width,
      dimensions.height,
      size.width,
      size.height,
    );
    viewMode = "custom";
    setZoomOutput();
    return scheduled ? scheduleRefresh() : refresh();
  }

  function zoomAtCenter(factor) {
    const size = viewportSize(elements.stage);
    return zoomAtPoint(factor, { x: size.width / 2, y: size.height / 2 });
  }

  function isActive() {
    return document.activeElement === elements.stage;
  }

  function inspectScreenPoint(point) {
    if (!view || !dimensions) return;
    const size = viewportSize(elements.stage);
    const display = displayRect(
      view,
      dimensions.width,
      dimensions.height,
      size.width,
      size.height,
    );
    if (point.x < display.left || point.x > display.left + display.width ||
        point.y < display.top || point.y > display.top + display.height) return;
    const source = screenToImage(
      view,
      point.x,
      point.y,
      dimensions.width,
      dimensions.height,
      size.width,
      size.height,
    );
    return inspectPixel(
      clampPixel(source.x, dimensions.width),
      clampPixel(source.y, dimensions.height),
    );
  }

  function inspectPixel(x, y) {
    if (!dimensions || activeGeneration == null) return;
    const selectedX = clampPixel(x, dimensions.width);
    const selectedY = clampPixel(y, dimensions.height);
    if (elements.selection) {
      elements.selection.dataset.x = String(selectedX);
      elements.selection.dataset.y = String(selectedY);
    }
    positionInspection();
    return onInspect(selectedX, selectedY);
  }

  function syncCursorMode() {
    if (gesture?.kind === "box" || boxMode) {
      elements.stage.dataset.viewportMode = "box";
    } else if (gesture?.dragged || gesture?.kind === "forced-pan") {
      elements.stage.dataset.viewportMode = "panning";
    } else if (spaceHeld) {
      elements.stage.dataset.viewportMode = "pan";
    } else {
      elements.stage.dataset.viewportMode = "inspect";
    }
  }

  function updateSelection(point) {
    if (!gesture || gesture.kind !== "box") return;
    const left = Math.min(gesture.start.x, point.x);
    const top = Math.min(gesture.start.y, point.y);
    const width = Math.abs(point.x - gesture.start.x);
    const height = Math.abs(point.y - gesture.start.y);
    const rect = selectionToImageRect(
      view,
      gesture.start,
      point,
      dimensions.width,
      dimensions.height,
      viewportSize(elements.stage).width,
      viewportSize(elements.stage).height,
    );
    elements.selection.style.left = `${left}px`;
    elements.selection.style.top = `${top}px`;
    elements.selection.style.width = `${width}px`;
    elements.selection.style.height = `${height}px`;
    elements.selection.hidden = false;
    if (elements.selectionLabel) {
      const right = rect.x + Math.max(0, rect.width - 1);
      const bottom = rect.y + Math.max(0, rect.height - 1);
      elements.selectionLabel.textContent =
        `View only · X ${rect.x}–${right} · Y ${rect.y}–${bottom} · ${rect.width}×${rect.height} px`;
      elements.selectionLabel.hidden = false;
    }
  }

  function fitSelection(point) {
    const size = viewportSize(elements.stage);
    const rect = selectionToImageRect(
      view,
      gesture.start,
      point,
      dimensions.width,
      dimensions.height,
      size.width,
      size.height,
    );
    if (rect.width <= 0 || rect.height <= 0) return;
    view = normalizedView({
      scale: Math.min(MAX_SCALE, size.width / rect.width, size.height / rect.height),
      centerX: rect.x + rect.width / 2,
      centerY: rect.y + rect.height / 2,
    });
    viewMode = "custom";
    setZoomOutput();
    return refresh();
  }

  function onPointerDown(event) {
    if (destroyed || !view || !dimensions || event.button !== 0 ||
        hasForbiddenModifier(event) || isControlTarget(event.target)) return;
    elements.stage.focus();
    const point = localPoint(elements.stage, event);
    const kind = event.shiftKey || boxMode ? "box" : (spaceHeld ? "forced-pan" : "pan");
    gesture = {
      pointerId: event.pointerId,
      kind,
      start: point,
      last: point,
      dragged: false,
    };
    elements.stage.setPointerCapture?.(event.pointerId);
    syncCursorMode();
  }

  function onPointerMove(event) {
    if (!gesture || event.pointerId !== gesture.pointerId || !view || !dimensions) return;
    const point = localPoint(elements.stage, event);
    if (!gesture.dragged && distance(gesture.start, point) > DRAG_THRESHOLD) {
      gesture.dragged = true;
    }
    if (!gesture.dragged) return;

    if (gesture.kind === "box") {
      updateSelection(point);
      return;
    }

    const size = viewportSize(elements.stage);
    view = panBy(
      view,
      point.x - gesture.last.x,
      point.y - gesture.last.y,
      dimensions.width,
      dimensions.height,
      size.width,
      size.height,
    );
    viewMode = "custom";
    gesture.last = point;
    setZoomOutput();
    syncCursorMode();
    return scheduleRefresh();
  }

  function onPointerUp(event) {
    if (!gesture || event.pointerId !== gesture.pointerId) return;
    const finished = gesture;
    const point = localPoint(elements.stage, event);
    if (elements.stage.hasPointerCapture?.(event.pointerId)) {
      elements.stage.releasePointerCapture(event.pointerId);
    }
    let result;
    if (!finished.dragged && finished.kind !== "forced-pan") {
      result = inspectScreenPoint(point);
    } else if (finished.dragged && finished.kind === "box") {
      result = fitSelection(point);
    }
    gesture = null;
    if (elements.selection) elements.selection.hidden = true;
    if (elements.selectionLabel) elements.selectionLabel.hidden = true;
    syncCursorMode();
    return result;
  }

  function onWheel(event) {
    if (!isActive() || !view || !dimensions || hasForbiddenModifier(event) ||
        isControlTarget(event.target)) return;
    event.preventDefault();
    const factor = event.deltaY < 0 ? ZOOM_FACTOR : 1 / ZOOM_FACTOR;
    return zoomAtPoint(factor, localPoint(elements.stage, event), true);
  }

  function onDoubleClick(event) {
    if (!isActive() || !view || !dimensions || hasForbiddenModifier(event) ||
        isControlTarget(event.target)) return;
    event.preventDefault();
    if (Math.abs(view.scale - 1) < 1e-9) return fit();
    return zoomAtPoint(1 / view.scale, localPoint(elements.stage, event));
  }

  function onKeyDown(event) {
    if (!isActive() || !view || !dimensions || hasForbiddenModifier(event) ||
        isControlTarget(event.target)) return;
    const size = viewportSize(elements.stage);
    const center = { x: size.width / 2, y: size.height / 2 };
    if (event.key === "Escape" && gesture) {
      event.preventDefault();
      cancelGesture();
      return;
    }
    if (event.code === "Space" || event.key === " ") {
      event.preventDefault();
      spaceHeld = true;
      syncCursorMode();
      return;
    }
    if (event.key === "+" || event.code === "NumpadAdd") {
      event.preventDefault();
      return zoomAtPoint(2 ** ZOOM_STEPS, center);
    }
    if (event.key === "-" || event.code === "NumpadSubtract") {
      event.preventDefault();
      return zoomAtPoint(2 ** -ZOOM_STEPS, center);
    }
    if (event.key === "0") {
      event.preventDefault();
      return fit();
    }
    if (event.key === "1") {
      event.preventDefault();
      return oneToOne();
    }

    const selected = inspectionCoordinate() || {
      x: Math.floor(dimensions.width / 2),
      y: Math.floor(dimensions.height / 2),
    };
    const step = event.shiftKey ? 10 : 1;
    if (event.key === "ArrowLeft") selected.x -= step;
    else if (event.key === "ArrowRight") selected.x += step;
    else if (event.key === "ArrowUp") selected.y -= step;
    else if (event.key === "ArrowDown") selected.y += step;
    else if (event.key === "Home") {
      selected.x = 0;
      selected.y = 0;
    } else if (event.key === "End") {
      selected.x = dimensions.width - 1;
      selected.y = dimensions.height - 1;
    } else if (event.key !== "Enter") {
      return;
    }
    event.preventDefault();
    return inspectPixel(selected.x, selected.y);
  }

  function onKeyUp(event) {
    if (!isActive() || isControlTarget(event.target) ||
        (event.code !== "Space" && event.key !== " ")) return;
    if (!hasForbiddenModifier(event)) event.preventDefault();
    spaceHeld = false;
    syncCursorMode();
  }

  function toggleBoxMode() {
    boxMode = !boxMode;
    elements.boxZoom?.setAttribute("aria-pressed", String(boxMode));
    syncCursorMode();
  }

  function retry() {
    return refresh();
  }

  function onResize() {
    if (destroyed || !dimensions || !view) return;
    cancelGesture();
    if (viewMode === "fit") resetToFit();
    else {
      view = normalizedView(view);
      setZoomOutput();
    }
    return scheduleRefresh();
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    ++renderSequence;
    cancelScheduledRender();
    cancelRequests();
    cancelGesture();
    clearLayers();
    resizeObserver?.disconnect();
    resizeObserver = null;
    listeners.splice(0).forEach((remove) => remove());
  }

  elements.retry.hidden = true;
  if (elements.selectionLabel) elements.selectionLabel.hidden = true;
  syncCursorMode();
  listen(elements.retry, "click", retry);
  listen(elements.zoomOut, "click", () => zoomAtCenter(1 / ZOOM_FACTOR));
  listen(elements.zoomIn, "click", () => zoomAtCenter(ZOOM_FACTOR));
  listen(elements.fit, "click", fit);
  listen(elements.oneToOne, "click", oneToOne);
  listen(elements.boxZoom, "click", toggleBoxMode);
  listen(elements.stage, "pointerdown", onPointerDown);
  listen(elements.stage, "pointermove", onPointerMove);
  listen(elements.stage, "pointerup", onPointerUp);
  listen(elements.stage, "pointercancel", cancelGesture);
  listen(elements.stage, "wheel", onWheel, { passive: false });
  listen(elements.stage, "dblclick", onDoubleClick);
  listen(elements.stage, "keydown", onKeyDown);
  listen(elements.stage, "keyup", onKeyUp);
  listen(elements.stage, "blur", () => {
    spaceHeld = false;
    cancelGesture();
  });
  if (typeof ResizeObserver !== "undefined") {
    resizeObserver = new ResizeObserver(onResize);
    resizeObserver.observe(elements.stage);
  }

  return {
    setGeneration,
    setStretch,
    refresh,
    fit,
    oneToOne,
    destroy,
  };
}
