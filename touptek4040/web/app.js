import { eventStream, getJson, mutate } from "./api.js";
import { bindImageViewer, setGeneration } from "./image-viewer.js";
import { authoritativeReadout } from "./generation-contract.js";
import { bindLogs, refreshLogs } from "./log-view.js";
import { bindSettings, readSettings, writeSettings } from "./settings-sync.js";

const byId = (id) => document.getElementById(id);
let currentState = null;
let events;

function toast(message, isError = false) {
  const item = document.createElement("div");
  item.className = `toast${isError ? " error" : ""}`;
  item.textContent = message;
  byId("toast-region").append(item);
  setTimeout(() => item.remove(), 4200);
}

function applyState(state) {
  currentState = state;
  const stateText = state.cameraState[0].toUpperCase() + state.cameraState.slice(1);
  byId("status-connection").textContent = state.connecting ? "Changing…" : (state.connected ? "Online" : "Offline");
  byId("status-connection").dataset.tone = state.connected ? "active" : "muted";
  byId("status-state").textContent = stateText;
  byId("status-state").dataset.tone = state.cameraState === "error" ? "danger" :
    (state.cameraState === "exposing" ? "active" : "muted");
  byId("status-temperature").textContent = state.temperature == null ? "—" : `${state.temperature.toFixed(1)} °C`;
  byId("status-serial").textContent = state.serial;
  byId("mobile-serial").textContent = state.serial;
  byId("connection-dot").classList.toggle("connected", state.connected);
  byId("connect-button").querySelector("span").textContent = state.connected ? "Disconnect" : "Connect";
  byId("connect-button").disabled = state.connecting;
  byId("preview-button").disabled = !state.connected || state.cameraState !== "idle";
  byId("preview-button").querySelector("span").textContent = state.preview ? "Stop preview" : "Start preview";
  byId("expose-button").disabled = !state.connected || !["idle", "error"].includes(state.cameraState);
  byId("abort-button").disabled = !state.connected || state.cameraState !== "exposing";
  byId("apply-settings").disabled = !state.connected || state.cameraState !== "idle";
  byId("status-readout").textContent = authoritativeReadout(state);

  const exposing = state.cameraState === "exposing";
  byId("exposure-progress").hidden = !exposing;
  const percent = state.percentCompleted || 0;
  byId("progress-fill").style.transform = `scaleX(${percent / 100})`;
  byId("progress-label").textContent = `${percent}%`;
  setGeneration(state.generation, state.image);
  if (state.error) toast(state.error, true);
}

async function refreshState() {
  applyState(await getJson("/web/v1/state"));
}

async function action(path, body) {
  try {
    await mutate(path, body);
    if (path === "/web/v1/connection" && body.connected) {
      writeSettings(await getJson("/web/v1/settings"));
    }
    await refreshState();
    await refreshLogs();
  } catch (error) {
    toast(error.message, true);
    await refreshState().catch(() => {});
  }
}

function bindActions() {
  byId("connect-button").addEventListener("click", () => {
    action("/web/v1/connection", { connected: !currentState?.connected });
  });
  byId("preview-button").addEventListener("click", () => {
    action("/web/v1/preview", { enabled: !currentState?.preview });
  });
  byId("expose-button").addEventListener("click", () => {
    action("/web/v1/exposure", { duration: Number(byId("exposure-duration").value) });
  });
  byId("abort-button").addEventListener("click", () => action("/web/v1/abort", {}));
  bindSettings(
    (settings) => {
      if (settings) {
        writeSettings(settings);
      }
      refreshLogs().catch(() => {});
    },
    (error) => toast(error.message, true),
  );
}

async function start() {
  bindActions();
  bindImageViewer();
  bindLogs((error) => toast(error.message, true));
  try {
    const settings = await getJson("/web/v1/settings");
    writeSettings(settings);
    await refreshState();
  } catch (error) {
    toast(error.message, true);
  }
  events = eventStream(
    applyState,
    () => setTimeout(() => refreshState().catch(() => {}), 1000),
  );
  setInterval(() => {
    refreshState().catch(() => {});
    refreshLogs().catch(() => {});
  }, 5000);
}

window.addEventListener("beforeunload", () => events?.close());
start();
