import { getJson } from "./api.js";

const byId = (id) => document.getElementById(id);
let entries = [];
let paused = false;

function render() {
  if (paused) return;
  const list = byId("log-list");
  const level = byId("log-level").value;
  const visible = level === "all" ? entries : entries.filter((entry) => entry.level === level);
  list.replaceChildren();
  visible.forEach((entry) => {
    const item = document.createElement("li");
    item.dataset.level = entry.level;
    const time = document.createElement("time");
    time.dateTime = entry.timestamp;
    time.textContent = entry.timestamp;
    const severity = document.createElement("span");
    severity.className = "log-level";
    severity.textContent = entry.level;
    const message = document.createElement("span");
    message.className = "log-message";
    message.textContent = entry.message;
    item.append(time, severity, message);
    list.append(item);
  });
  byId("log-empty").hidden = visible.length > 0;
  if (visible.length) list.scrollTop = list.scrollHeight;
}

export async function refreshLogs() {
  const body = await getJson("/web/v1/logs");
  entries = body.entries;
  render();
}

export function bindLogs(onError) {
  byId("log-level").addEventListener("change", render);
  byId("pause-logs").addEventListener("click", (event) => {
    paused = !paused;
    event.currentTarget.textContent = paused ? "Resume" : "Pause";
    if (!paused) render();
  });
  byId("export-logs").addEventListener("click", () => {
    const content = entries.map((entry) =>
      `${entry.timestamp}\t${entry.level.toUpperCase()}\t${entry.message}`
    ).join("\n");
    const url = URL.createObjectURL(new Blob([content], { type: "text/plain" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "touptek4040-console.log";
    link.click();
    URL.revokeObjectURL(url);
  });
  refreshLogs().catch(onError);
}
