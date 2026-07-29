import { getJson } from "./api.js";

const byId = (id) => document.getElementById(id);
let activeGeneration = null;
let activeDimensions = null;
let selectedPixel = null;
let renderSequence = 0;

function stretchQuery() {
  return new URLSearchParams({
    black: byId("black-point").value,
    white: byId("white-point").value,
    gamma: byId("gamma").value,
    maxWidth: "1600",
    maxHeight: "1200",
  });
}

export function setGeneration(generation, dimensions = null) {
  activeDimensions = dimensions;
  if (generation !== activeGeneration) selectedPixel = null;
  if (generation === activeGeneration) return;
  activeGeneration = generation;
  byId("generation-label").textContent = generation ? `Generation ${generation}` : "No generation";
  byId("fits-link").setAttribute("aria-disabled", generation ? "false" : "true");
  if (generation) {
    renderImage();
    refreshAnalysis();
  } else {
    byId("science-image").hidden = true;
    byId("image-empty").hidden = false;
  }
}

export async function renderImage() {
  if (!activeGeneration) return;
  const sequence = ++renderSequence;
  const image = byId("science-image");
  byId("image-empty").hidden = true;
  byId("image-error").hidden = true;
  byId("image-loading").hidden = false;
  const candidate = new Image();
  candidate.onload = () => {
    if (sequence !== renderSequence) return;
    image.src = candidate.src;
    image.hidden = false;
    byId("image-loading").hidden = true;
  };
  candidate.onerror = () => {
    if (sequence !== renderSequence) return;
    byId("image-loading").hidden = true;
    byId("image-error").hidden = false;
  };
  candidate.src = `/web/v1/viewport.png?${stretchQuery()}&generation=${activeGeneration}`;
}

export async function refreshAnalysis() {
  if (!activeGeneration) return;
  try {
    const stats = await getJson("/web/v1/statistics");
    if (stats.generation !== activeGeneration) return;
    byId("stat-min").textContent = String(stats.minimum);
    byId("stat-max").textContent = String(stats.maximum);
    byId("stat-mean").textContent = stats.mean.toFixed(2);
    byId("stat-sigma").textContent = stats.standardDeviation.toFixed(2);
    byId("stat-fwhm-x").textContent = stats.fwhm.x == null ? "—" : stats.fwhm.x.toFixed(2);
    byId("stat-fwhm-y").textContent = stats.fwhm.y == null ? "—" : stats.fwhm.y.toFixed(2);
    byId("histogram-range").textContent = `${stats.minimum} — ${stats.maximum}`;
    drawHistogram(stats.histogram);
  } catch (error) {
    byId("image-error-copy").textContent = error.message;
  }
}

function drawHistogram(values) {
  const canvas = byId("histogram");
  const context = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  context.clearRect(0, 0, width, height);
  const maximum = Math.max(...values, 1);
  context.fillStyle = "#58b8d1";
  values.forEach((value, index) => {
    const bar = Math.max(1, Math.round((value / maximum) * (height - 5)));
    const x = Math.floor((index / values.length) * width);
    const next = Math.ceil(((index + 1) / values.length) * width);
    context.fillRect(x, height - bar, Math.max(1, next - x), bar);
  });
}

function bindStretch() {
  const controls = [
    ["black-point", "black-value", (value) => value],
    ["white-point", "white-value", (value) => value],
    ["gamma", "gamma-value", (value) => Number(value).toFixed(2)],
  ];
  let timer;
  controls.forEach(([inputId, outputId, format]) => {
    const input = byId(inputId);
    input.addEventListener("input", () => {
      byId(outputId).textContent = format(input.value);
      clearTimeout(timer);
      timer = setTimeout(renderImage, 120);
    });
  });
  byId("auto-stretch").addEventListener("click", async () => {
    const stats = await getJson("/web/v1/statistics");
    const black = Math.max(0, Math.floor(stats.mean - stats.standardDeviation * 2));
    const white = Math.max(black + 1, Math.min(4095, Math.ceil(stats.mean + stats.standardDeviation * 5)));
    byId("black-point").value = String(black);
    byId("black-value").textContent = String(black);
    byId("white-point").value = String(white);
    byId("white-value").textContent = String(white);
    renderImage();
  });
}

function bindPixelInspection() {
  const stage = byId("image-stage");
  const image = byId("science-image");
  async function inspectPixel(x, y) {
    if (image.hidden || !activeGeneration || !activeDimensions) return;
    selectedPixel = { x, y };
    const pixel = await getJson(`/web/v1/pixel?x=${x}&y=${y}`);
    byId("pixel-readout").textContent = `Pixel ${pixel.x}, ${pixel.y} · ${pixel.value} ADU`;
    const bounds = image.getBoundingClientRect();
    const stageBounds = stage.getBoundingClientRect();
    const cursor = byId("pixel-cursor");
    cursor.style.left = `${bounds.left - stageBounds.left + ((x + 0.5) / activeDimensions.width) * bounds.width}px`;
    cursor.style.top = `${bounds.top - stageBounds.top + ((y + 0.5) / activeDimensions.height) * bounds.height}px`;
    cursor.hidden = false;
  }
  stage.addEventListener("click", async (event) => {
    if (image.hidden || !activeGeneration || !activeDimensions) return;
    const bounds = image.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right ||
        event.clientY < bounds.top || event.clientY > bounds.bottom) return;
    const width = activeDimensions.width;
    const height = activeDimensions.height;
    const x = Math.min(width - 1, Math.max(0, Math.floor(((event.clientX - bounds.left) / bounds.width) * width)));
    const y = Math.min(height - 1, Math.max(0, Math.floor(((event.clientY - bounds.top) / bounds.height) * height)));
    await inspectPixel(x, y);
  });
  stage.addEventListener("keydown", async (event) => {
    if (!activeGeneration || !activeDimensions) return;
    const current = selectedPixel || {
      x: Math.floor(activeDimensions.width / 2),
      y: Math.floor(activeDimensions.height / 2),
    };
    const step = event.shiftKey ? 10 : 1;
    const next = { ...current };
    if (event.key === "ArrowLeft") next.x -= step;
    else if (event.key === "ArrowRight") next.x += step;
    else if (event.key === "ArrowUp") next.y -= step;
    else if (event.key === "ArrowDown") next.y += step;
    else if (event.key === "Home") {
      next.x = 0;
      next.y = 0;
    } else if (event.key === "End") {
      next.x = activeDimensions.width - 1;
      next.y = activeDimensions.height - 1;
    }
    else if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    next.x = Math.min(activeDimensions.width - 1, Math.max(0, next.x));
    next.y = Math.min(activeDimensions.height - 1, Math.max(0, next.y));
    await inspectPixel(next.x, next.y);
  });
}

export function bindImageViewer() {
  bindStretch();
  bindPixelInspection();
  byId("refresh-analysis").addEventListener("click", refreshAnalysis);
}
