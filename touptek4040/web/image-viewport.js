import { getBlob } from "./api.js";
import { generationUrl } from "./generation-contract.js";
import { MAX_SCALE, fitView, planCropSegments } from "./viewport-math.js";

const DEFAULT_STRETCH = { black: 0, white: 4095, gamma: 1 };

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

export function createImageViewport({
  elements,
  fetchBlob = getBlob,
  makeObjectUrl = (blob) => URL.createObjectURL(blob),
  revokeObjectUrl = (url) => URL.revokeObjectURL(url),
  onInspect = () => {},
  onGenerationConflict = () => {},
  onError = () => {},
} = {}) {
  if (!elements?.stage || !elements.layerHost || !elements.retry) {
    throw new TypeError("viewport elements are required");
  }

  let activeGeneration = null;
  let dimensions = null;
  let stretch = DEFAULT_STRETCH;
  let view = null;
  let renderSequence = 0;
  let destroyed = false;
  let ownedUrls = [];
  let requestControllers = new Set();

  function cancelRequests() {
    requestControllers.forEach((controller) => controller.abort());
    requestControllers = new Set();
  }

  function revoke(urls) {
    urls.forEach((url) => revokeObjectUrl(url));
  }

  function clearLayers() {
    revoke(ownedUrls);
    ownedUrls = [];
    elements.layerHost.replaceChildren();
  }

  function clearOutOfBoundsInspection() {
    const inspection = elements.selection?.dataset;
    const x = Number(inspection?.x);
    const y = Number(inspection?.y);
    const inBounds = Number.isInteger(x) && Number.isInteger(y) &&
      x >= 0 && y >= 0 && x < dimensions.width && y < dimensions.height;
    if (!inBounds) {
      if (elements.selection) elements.selection.hidden = true;
      if (elements.selectionLabel) elements.selectionLabel.hidden = true;
    }
  }

  function setZoomOutput() {
    if (elements.zoomOutput) {
      elements.zoomOutput.textContent = view ? `${Math.round(view.scale * 100)}%` : "";
    }
  }

  function resetToFit() {
    if (!dimensions) {
      view = null;
      setZoomOutput();
      return;
    }
    const size = viewportSize(elements.stage);
    view = fitView(dimensions.width, dimensions.height, size.width, size.height);
    setZoomOutput();
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

  function setGeneration(generation, nextDimensions = null) {
    if (destroyed) return;
    const generationChanged = generation !== activeGeneration;
    const shapeChanged = !sameShape(dimensions, nextDimensions);
    if (generationChanged || shapeChanged) {
      ++renderSequence;
      cancelRequests();
    }

    activeGeneration = generation;
    dimensions = nextDimensions;
    if (generation == null) {
      clearLayers();
      view = null;
      elements.selection && (elements.selection.hidden = true);
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
    view = {
      scale: Math.min(MAX_SCALE, Math.max(fitted.scale, 1)),
      centerX: dimensions.width / 2,
      centerY: dimensions.height / 2,
    };
    setZoomOutput();
    return refresh();
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    ++renderSequence;
    cancelRequests();
    clearLayers();
    elements.retry.removeEventListener("click", retry);
  }

  function retry() {
    refresh();
  }

  elements.retry.hidden = true;
  elements.retry.addEventListener("click", retry);
  void onInspect;
  void elements.selectionLabel;

  return {
    setGeneration,
    setStretch,
    refresh,
    fit,
    oneToOne,
    destroy,
  };
}
