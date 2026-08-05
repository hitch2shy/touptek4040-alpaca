export const MAX_SCALE = 16;
export const MAX_SEGMENT_EDGE = 2048;

function assertPositiveFinite(...values) {
  if (values.some((value) => !Number.isFinite(value) || value <= 0)) {
    throw new TypeError("dimensions must be finite positive numbers");
  }
}

function assertFinite(...values) {
  if (values.some((value) => !Number.isFinite(value))) {
    throw new TypeError("coordinates must be finite numbers");
  }
}

function clampView(view, imageWidth, imageHeight, viewportWidth, viewportHeight) {
  assertPositiveFinite(imageWidth, imageHeight, viewportWidth, viewportHeight);
  if (!view || typeof view !== "object") {
    throw new TypeError("view must be an object");
  }
  assertPositiveFinite(view.scale);
  assertFinite(view.centerX, view.centerY);

  const fitScale = Math.min(
    viewportWidth / imageWidth,
    viewportHeight / imageHeight,
  );
  const scale = Math.min(Math.max(MAX_SCALE, fitScale), Math.max(fitScale, view.scale));
  const visibleWidth = Math.min(imageWidth, viewportWidth / scale);
  const visibleHeight = Math.min(imageHeight, viewportHeight / scale);
  const minCenterX = visibleWidth / 2;
  const maxCenterX = imageWidth - visibleWidth / 2;
  const minCenterY = visibleHeight / 2;
  const maxCenterY = imageHeight - visibleHeight / 2;

  return {
    scale,
    centerX: Math.min(maxCenterX, Math.max(minCenterX, view.centerX)),
    centerY: Math.min(maxCenterY, Math.max(minCenterY, view.centerY)),
  };
}

export function fitView(imageWidth, imageHeight, viewportWidth, viewportHeight) {
  assertPositiveFinite(imageWidth, imageHeight, viewportWidth, viewportHeight);
  const fitScale = Math.min(
    viewportWidth / imageWidth,
    viewportHeight / imageHeight,
  );
  return {
    scale: fitScale,
    centerX: imageWidth / 2,
    centerY: imageHeight / 2,
  };
}

export function visibleRect(view, imageWidth, imageHeight, viewportWidth, viewportHeight) {
  const bounded = clampView(view, imageWidth, imageHeight, viewportWidth, viewportHeight);
  const visibleWidth = Math.min(imageWidth, viewportWidth / bounded.scale);
  const visibleHeight = Math.min(imageHeight, viewportHeight / bounded.scale);
  const x = Math.min(
    imageWidth - visibleWidth,
    Math.max(0, bounded.centerX - visibleWidth / 2),
  );
  const y = Math.min(
    imageHeight - visibleHeight,
    Math.max(0, bounded.centerY - visibleHeight / 2),
  );

  return { x, y, width: visibleWidth, height: visibleHeight };
}

export function screenToImage(
  view,
  screenX,
  screenY,
  imageWidth,
  imageHeight,
  viewportWidth,
  viewportHeight,
) {
  assertFinite(screenX, screenY);
  const bounded = clampView(view, imageWidth, imageHeight, viewportWidth, viewportHeight);
  const visible = visibleRect(bounded, imageWidth, imageHeight, viewportWidth, viewportHeight);
  return {
    x: visible.x + screenX / bounded.scale,
    y: visible.y + screenY / bounded.scale,
  };
}

export function zoomAt(
  view,
  zoomSteps,
  screenX,
  screenY,
  imageWidth,
  imageHeight,
  viewportWidth,
  viewportHeight,
) {
  assertFinite(zoomSteps, screenX, screenY);
  const current = clampView(view, imageWidth, imageHeight, viewportWidth, viewportHeight);
  const pointed = screenToImage(
    current,
    screenX,
    screenY,
    imageWidth,
    imageHeight,
    viewportWidth,
    viewportHeight,
  );
  const fitScale = Math.min(
    viewportWidth / imageWidth,
    viewportHeight / imageHeight,
  );
  const scale = Math.min(
    Math.max(MAX_SCALE, fitScale),
    Math.max(fitScale, current.scale * 2 ** zoomSteps),
  );
  return clampView(
    {
      scale,
      centerX: pointed.x + (viewportWidth / 2 - screenX) / scale,
      centerY: pointed.y + (viewportHeight / 2 - screenY) / scale,
    },
    imageWidth,
    imageHeight,
    viewportWidth,
    viewportHeight,
  );
}

export function panBy(
  view,
  deltaX,
  deltaY,
  imageWidth,
  imageHeight,
  viewportWidth,
  viewportHeight,
) {
  assertFinite(deltaX, deltaY);
  const current = clampView(view, imageWidth, imageHeight, viewportWidth, viewportHeight);
  return clampView(
    {
      scale: current.scale,
      centerX: current.centerX - deltaX / current.scale,
      centerY: current.centerY - deltaY / current.scale,
    },
    imageWidth,
    imageHeight,
    viewportWidth,
    viewportHeight,
  );
}

export function selectionToImageRect(
  view,
  start,
  end,
  imageWidth,
  imageHeight,
  viewportWidth,
  viewportHeight,
) {
  if (!start || !end) throw new TypeError("selection corners are required");
  const first = screenToImage(
    view,
    start.x,
    start.y,
    imageWidth,
    imageHeight,
    viewportWidth,
    viewportHeight,
  );
  const second = screenToImage(
    view,
    end.x,
    end.y,
    imageWidth,
    imageHeight,
    viewportWidth,
    viewportHeight,
  );
  const x = Math.min(imageWidth, Math.max(0, Math.floor(Math.min(first.x, second.x))));
  const y = Math.min(imageHeight, Math.max(0, Math.floor(Math.min(first.y, second.y))));
  const right = Math.min(imageWidth, Math.max(0, Math.ceil(Math.max(first.x, second.x))));
  const bottom = Math.min(imageHeight, Math.max(0, Math.ceil(Math.max(first.y, second.y))));

  return { x, y, width: Math.max(0, right - x), height: Math.max(0, bottom - y) };
}

export function planCropSegments(
  view,
  imageWidth,
  imageHeight,
  viewportWidth,
  viewportHeight,
  maxEdge = MAX_SEGMENT_EDGE,
) {
  assertPositiveFinite(maxEdge);
  if (!Number.isSafeInteger(maxEdge)) {
    throw new TypeError("maxEdge must be a positive safe integer");
  }
  const bounded = clampView(view, imageWidth, imageHeight, viewportWidth, viewportHeight);
  const visible = visibleRect(bounded, imageWidth, imageHeight, viewportWidth, viewportHeight);
  const left = Math.floor(visible.x);
  const top = Math.floor(visible.y);
  const right = Math.ceil(visible.x + visible.width);
  const bottom = Math.ceil(visible.y + visible.height);
  const segments = [];

  for (let y = top; y < bottom; y += maxEdge) {
    const height = Math.min(maxEdge, bottom - y);
    for (let x = left; x < right; x += maxEdge) {
      const width = Math.min(maxEdge, right - x);
      segments.push({
        x,
        y,
        width,
        height,
        left: (x - visible.x) * bounded.scale,
        top: (y - visible.y) * bounded.scale,
        displayWidth: width * bounded.scale,
        displayHeight: height * bounded.scale,
      });
    }
  }
  return segments;
}
