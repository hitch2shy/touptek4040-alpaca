export function normalizeStretch(maxADU, black, white) {
  const ceiling = Number(maxADU);
  if (!Number.isInteger(ceiling) || ceiling < 1) {
    throw new TypeError("maxADU must be a positive integer");
  }
  const normalizedBlack = Math.min(
    ceiling - 1,
    Math.max(0, Math.trunc(Number(black) || 0)),
  );
  const normalizedWhite = Math.min(
    ceiling,
    Math.max(normalizedBlack + 1, Math.trunc(Number(white) || ceiling)),
  );
  return { black: normalizedBlack, white: normalizedWhite };
}

export function autoStretch(maxADU, statistics) {
  const black = Math.floor(
    statistics.mean - statistics.standardDeviation * 2,
  );
  const white = Math.ceil(
    statistics.mean + statistics.standardDeviation * 5,
  );
  return normalizeStretch(maxADU, black, white);
}

export function generationUrl(path, generation, parameters = {}) {
  if (!Number.isInteger(generation) || generation < 1) {
    throw new TypeError("generation must be a positive integer");
  }
  const query = new URLSearchParams({ generation: String(generation) });
  Object.entries(parameters).forEach(([name, value]) => {
    query.set(name, String(value));
  });
  return `${path}?${query}`;
}

export function authoritativeReadout(state) {
  return state.image?.readoutMode || state.settings?.readoutMode || "—";
}

export function isGenerationConflict(error) {
  return error?.status === 409;
}
