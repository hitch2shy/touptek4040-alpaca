import { mutate } from "./api.js";

const byId = (id) => document.getElementById(id);
let confirmedHeaterLevel = null;
let renderedHeaterMaximum = null;
let heaterWritable = false;

export function readSettings() {
  return {
    binning: Number(byId("binning").value),
    readoutMode: byId("readout-mode").value,
    gain: Number(byId("gain").value),
    roi: {
      x: Number(byId("roi-x").value),
      y: Number(byId("roi-y").value),
      width: Number(byId("roi-width").value),
      height: Number(byId("roi-height").value),
    },
  };
}

export function writeSettings(settings) {
  byId("binning").value = String(settings.binning);
  byId("readout-mode").value = settings.readoutMode;
  byId("gain").value = String(settings.gain);
  byId("gain-value").textContent = String(settings.gain);
  byId("roi-x").value = String(settings.roi.x);
  byId("roi-y").value = String(settings.roi.y);
  byId("roi-width").value = String(settings.roi.width);
  byId("roi-height").value = String(settings.roi.height);
  byId("target-temperature").value = String(settings.targetTemperature);
  byId("cooler-on").checked = settings.coolerOn;
}

export function writeHeaterState(state) {
  const heater = byId("heater-level");
  const status = byId("heater-status");
  const maximum = Number.isInteger(state.heaterMaxLevel)
    && state.heaterMaxLevel >= 0
    ? state.heaterMaxLevel
    : null;
  if (renderedHeaterMaximum !== maximum) {
    const options = [];
    const off = document.createElement("option");
    off.value = "0";
    off.textContent = "Off";
    options.push(off);
    for (let level = 1; level <= (maximum ?? 0); level += 1) {
      const option = document.createElement("option");
      option.value = String(level);
      option.textContent = String(level);
      options.push(option);
    }
    heater.replaceChildren(...options);
    renderedHeaterMaximum = maximum;
  }

  const confirmed = Number.isInteger(state.heaterLevel)
    && state.heaterLevel >= 0
    && (maximum == null || state.heaterLevel <= maximum)
    ? state.heaterLevel
    : null;
  if (confirmed != null) {
    confirmedHeaterLevel = confirmed;
    heater.value = String(confirmed);
  }
  heaterWritable = state.heaterWritable === true;
  heater.disabled = !heaterWritable;

  if (!state.connected) {
    status.textContent = "Not connected";
  } else if (maximum == null) {
    status.textContent = "Read failed";
  } else if (!state.heaterSupported) {
    status.textContent = "Not supported";
  } else if (confirmed == null) {
    status.textContent = "Read failed";
  } else if (!heaterWritable) {
    status.textContent = state.cameraState === "exposing"
      ? `Level ${confirmed} · Exposing`
      : `Level ${confirmed} · Read only`;
  } else {
    status.textContent = confirmed === 0 ? "Off" : `Level ${confirmed}`;
  }
}

export function bindSettings(onApplied, onError) {
  const gain = byId("gain");
  gain.addEventListener("input", () => {
    byId("gain-value").textContent = gain.value;
  });
  byId("binning").addEventListener("change", () => {
    const binning = Number(byId("binning").value);
    let extent = Math.floor(4096 / binning);
    if ((extent * binning) % 2 !== 0) extent -= 1;
    byId("roi-width").value = String(extent);
    byId("roi-height").value = String(extent);
  });
  byId("apply-settings").addEventListener("click", async () => {
    const message = byId("settings-message");
    message.classList.remove("error");
    message.textContent = "Applying…";
    try {
      const settings = await mutate("/web/v1/settings", readSettings());
      message.textContent = "Settings synchronized";
      onApplied(settings);
    } catch (error) {
      message.classList.add("error");
      message.textContent = error.message;
      onError(error);
    }
  });
  async function applyCooling(field) {
    const message = byId("settings-message");
    message.classList.remove("error");
    message.textContent = "Applying cooling…";
    try {
      const body = field === "targetTemperature"
        ? { targetTemperature: Number(byId("target-temperature").value) }
        : { coolerOn: byId("cooler-on").checked };
      const cooling = await mutate("/web/v1/cooling", body);
      byId("target-temperature").value = String(cooling.targetTemperature);
      byId("cooler-on").checked = cooling.coolerOn;
      message.textContent = "Cooling synchronized";
      onApplied();
    } catch (error) {
      message.classList.add("error");
      message.textContent = error.message;
      onError(error);
    }
  }
  byId("target-temperature").addEventListener(
    "change",
    () => applyCooling("targetTemperature"),
  );
  byId("cooler-on").addEventListener(
    "change",
    () => applyCooling("coolerOn"),
  );
  byId("heater-level").addEventListener("change", async () => {
    const heater = byId("heater-level");
    const status = byId("heater-status");
    const previous = confirmedHeaterLevel;
    heater.disabled = true;
    status.textContent = "Applying…";
    try {
      const result = await mutate(
        "/web/v1/heater",
        { heaterLevel: Number(heater.value) },
      );
      if (
        !Number.isInteger(result.heaterLevel)
        || result.heaterLevel < 0
      ) {
        throw new Error("Camera returned an invalid heater level");
      }
      confirmedHeaterLevel = result.heaterLevel;
      heater.value = String(result.heaterLevel);
      status.textContent = result.heaterLevel === 0
        ? "Off"
        : `Level ${result.heaterLevel}`;
      onApplied();
    } catch (error) {
      if (previous != null) heater.value = String(previous);
      status.textContent = previous == null
        ? "Write failed"
        : `Write failed · Level ${previous}`;
      onError(error);
    } finally {
      heater.disabled = !heaterWritable;
    }
  });
}
