import { getJson } from "./api.js";
import {
  autoStretch,
  generationUrl,
  isGenerationConflict,
  normalizeStretch,
} from "./generation-contract.js";
import { createImageViewport } from "./image-viewport.js";

const byId = (id) => document.getElementById(id);
let activeGeneration = null;
let activeDimensions = null;
let viewportController = null;
let renderSequence = 0;
let analysisController = null;
let pixelController = null;
let stretchTimer = null;
let viewerListeners = [];

function listen(element, name, handler) {
  element.addEventListener(name, handler);
  viewerListeners.push(() => element.removeEventListener(name, handler));
}

function readStretch() {
  return {
    black: Number(byId("black-point").value),
    white: Number(byId("white-point").value),
    gamma: Number(byId("gamma").value),
  };
}

function syncStretch(maxADU) {
  const blackInput = byId("black-point");
  const whiteInput = byId("white-point");
  const stretch = normalizeStretch(maxADU, blackInput.value, whiteInput.value);
  blackInput.max = String(maxADU - 1);
  whiteInput.max = String(maxADU);
  blackInput.value = String(stretch.black);
  whiteInput.value = String(stretch.white);
  byId("black-value").textContent = String(stretch.black);
  byId("white-value").textContent = String(stretch.white);
}

function hideViewportError() {
  byId("image-error").hidden = true;
}

function showViewportError(error) {
  byId("image-loading").hidden = true;
  byId("image-error-copy").textContent = error.message;
  byId("image-error").hidden = false;
}

async function recoverGeneration(error) {
  if (!isGenerationConflict(error)) return false;
  const state = await getJson("/web/v1/state");
  setGeneration(state.generation, state.image);
  return true;
}

async function inspectPixel(x, y) {
  if (activeGeneration == null || !activeDimensions) return;
  pixelController?.abort();
  const controller = new AbortController();
  pixelController = controller;
  const generation = activeGeneration;
  try {
    const pixel = await getJson(
      generationUrl("/web/v1/pixel", generation, { x, y }),
      controller.signal,
    );
    if (controller.signal.aborted || pixel.generation !== activeGeneration) return;
    byId("pixel-readout").textContent =
      `Pixel ${pixel.x}, ${pixel.y} · ${pixel.value} ADU`;
  } catch (error) {
    if (error?.name === "AbortError") return;
    if (!await recoverGeneration(error)) showViewportError(error);
  } finally {
    if (pixelController === controller) pixelController = null;
  }
}

function viewportElements() {
  return {
    stage: byId("image-stage"),
    layerHost: byId("viewport-layer-host"),
    selection: byId("viewport-selection"),
    selectionLabel: byId("viewport-selection-label"),
    zoomOutput: byId("viewport-zoom"),
    retry: byId("viewport-retry"),
    zoomOut: byId("viewport-zoom-out"),
    zoomIn: byId("viewport-zoom-in"),
    fit: byId("viewport-fit"),
    oneToOne: byId("viewport-one-to-one"),
    boxZoom: byId("viewport-box-zoom"),
    pixelCursor: byId("pixel-cursor"),
  };
}

export function setGeneration(generation, dimensions = null) {
  const generationChanged = generation !== activeGeneration;
  const shapeChanged = dimensions?.width !== activeDimensions?.width ||
    dimensions?.height !== activeDimensions?.height;
  if (generationChanged || shapeChanged) {
    pixelController?.abort();
    pixelController = null;
  }
  activeGeneration = generation;
  activeDimensions = dimensions;
  if (dimensions?.maxADU != null) syncStretch(dimensions.maxADU);

  const hasGeneration = generation != null;
  byId("generation-label").textContent = hasGeneration
    ? `Generation ${generation}`
    : "No generation";
  byId("fits-link").setAttribute("aria-disabled", hasGeneration ? "false" : "true");
  if (hasGeneration) {
    byId("fits-link").href = generationUrl("/web/v1/image.fits", generation);
    byId("fits-link").download = "";
  } else {
    byId("fits-link").removeAttribute("href");
    byId("fits-link").removeAttribute("download");
  }

  viewportController?.setGeneration(generation, dimensions);
  const selectedX = Number(byId("viewport-selection").dataset.x);
  const selectedY = Number(byId("viewport-selection").dataset.y);
  const hasSelection = Number.isInteger(selectedX) && Number.isInteger(selectedY);
  if (!hasSelection) {
    byId("pixel-readout").textContent = "Pixel —";
  } else if (hasGeneration && (generationChanged || shapeChanged)) {
    void inspectPixel(selectedX, selectedY);
  }
  if (!hasGeneration) {
    ++renderSequence;
    analysisController?.abort();
    pixelController?.abort();
    byId("image-empty").hidden = false;
    byId("image-loading").hidden = true;
    hideViewportError();
    return;
  }

  byId("image-empty").hidden = true;
  if (generationChanged || shapeChanged) {
    void renderImage();
    void refreshAnalysis();
  }
}

export async function renderImage() {
  if (activeGeneration == null || !viewportController) return;
  const sequence = ++renderSequence;
  byId("image-empty").hidden = true;
  hideViewportError();
  byId("image-loading").hidden = false;
  viewportController.setStretch(readStretch());
  await viewportController.refresh();
  if (sequence === renderSequence) byId("image-loading").hidden = true;
}

export async function refreshAnalysis() {
  if (activeGeneration == null) return;
  analysisController?.abort();
  const controller = new AbortController();
  analysisController = controller;
  const generation = activeGeneration;
  try {
    const stats = await getJson(
      generationUrl("/web/v1/statistics", generation),
      controller.signal,
    );
    if (controller.signal.aborted || stats.generation !== activeGeneration) return;
    byId("stat-min").textContent = String(stats.minimum);
    byId("stat-max").textContent = String(stats.maximum);
    byId("stat-mean").textContent = stats.mean.toFixed(2);
    byId("stat-sigma").textContent = stats.standardDeviation.toFixed(2);
    byId("stat-fwhm-x").textContent = stats.fwhm.x == null ? "—" : stats.fwhm.x.toFixed(2);
    byId("stat-fwhm-y").textContent = stats.fwhm.y == null ? "—" : stats.fwhm.y.toFixed(2);
    byId("histogram-range").textContent = `${stats.minimum} — ${stats.maximum}`;
    drawHistogram(stats.histogram);
  } catch (error) {
    if (error?.name === "AbortError") return;
    if (!await recoverGeneration(error)) {
      byId("image-error-copy").textContent = error.message;
    }
  } finally {
    if (analysisController === controller) analysisController = null;
  }
}

function drawHistogram(values) {
  const canvas = byId("histogram");
  const context = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  context.clearRect(0, 0, width, height);
  const maximum = Math.max(...values, 1);
  values.forEach((value, index) => {
    const bar = Math.max(1, Math.round((value / maximum) * (height - 5)));
    const x = Math.floor((index / values.length) * width);
    const next = Math.ceil(((index + 1) / values.length) * width);
    context.fillStyle = "#58b8d1";
    context.fillRect(x, height - bar, Math.max(1, next - x), bar);
  });
}

function bindStretch() {
  const controls = [
    ["black-point", "black-value", (value) => value],
    ["white-point", "white-value", (value) => value],
    ["gamma", "gamma-value", (value) => Number(value).toFixed(2)],
  ];
  controls.forEach(([inputId, outputId, format]) => {
    const input = byId(inputId);
    listen(input, "input", () => {
      byId(outputId).textContent = format(input.value);
      clearTimeout(stretchTimer);
      stretchTimer = setTimeout(() => {
        stretchTimer = null;
        void renderImage();
      }, 120);
    });
  });

  listen(byId("auto-stretch"), "click", async () => {
    if (activeGeneration == null || !activeDimensions) return;
    try {
      const stats = await getJson(
        generationUrl("/web/v1/statistics", activeGeneration),
      );
      const stretch = autoStretch(activeDimensions.maxADU, stats);
      byId("black-point").value = String(stretch.black);
      byId("black-value").textContent = String(stretch.black);
      byId("white-point").value = String(stretch.white);
      byId("white-value").textContent = String(stretch.white);
      await renderImage();
    } catch (error) {
      if (!await recoverGeneration(error)) showViewportError(error);
    }
  });
}

export function bindImageViewer() {
  if (viewportController) destroyImageViewer();
  viewportController = createImageViewport({
    elements: viewportElements(),
    onInspect: inspectPixel,
    onGenerationConflict: (error) => void recoverGeneration(error),
    onError: showViewportError,
    onRendered: () => {
      hideViewportError();
      byId("image-loading").hidden = true;
    },
  });
  bindStretch();
  listen(byId("refresh-analysis"), "click", refreshAnalysis);
}

export function destroyImageViewer() {
  ++renderSequence;
  clearTimeout(stretchTimer);
  stretchTimer = null;
  analysisController?.abort();
  analysisController = null;
  pixelController?.abort();
  pixelController = null;
  viewportController?.destroy();
  viewportController = null;
  viewerListeners.splice(0).forEach((remove) => remove());
  activeGeneration = null;
  activeDimensions = null;
}
