"""Non-technical local diagnostics using the same single-owner coordinator."""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .camera_contract import ExposureSettings, ReadoutMode, Roi
from .camera_coordinator import CameraCoordinator
from .config import AppConfig
from .errors import CameraError, safe_exception_text

_LOG = logging.getLogger(__name__)
_OWNER_WARNING = (
    "Single-owner rule: close the Alpaca server before opening the real camera here. "
    "If another process owns it, diagnostics will stop without taking control."
)


@dataclass(frozen=True)
class DiagnosticSettings:
    duration: float
    gain: int
    binning: int
    mode: ReadoutMode
    roi: Roi


def run_diagnostic_gui(config: AppConfig) -> int:
    """Open a compact diagnostics window; no camera is touched before Connect."""

    import tkinter as tk
    from tkinter import messagebox, ttk

    from run_alpaca import build_coordinator, configure_logging

    configure_logging(config)
    try:
        coordinator, resolved = build_coordinator(config)
    except CameraError as error:
        messagebox.showerror(
            "ToupTek 4040 diagnostics",
            f"Camera enumeration failed.\n\n{safe_exception_text(error)}\n\n{_OWNER_WARNING}",
        )
        return 2

    root = tk.Tk()
    root.title("ToupTek 4040 Diagnostics")
    root.minsize(760, 560)
    events: queue.Queue[tuple[str, object]] = queue.Queue()
    status = tk.StringVar(value="Not connected")
    identity = tk.StringVar(
        value=(
            f"Source: {resolved.camera.source}    "
            f"Serial: {resolved.camera.serial or 'SIM-4040'}"
        )
    )
    mode = tk.StringVar(value=resolved.camera.readout_mode)
    gain = tk.StringVar(value=str(resolved.camera.gain or 100))
    binning = tk.StringVar(value="1")
    duration = tk.StringVar(value="0.1")
    roi_x, roi_y = tk.StringVar(value="0"), tk.StringVar(value="0")
    roi_w, roi_h = tk.StringVar(value="4096"), tk.StringVar(value="4096")
    target = tk.StringVar(value="0.0")
    cooler = tk.BooleanVar(value=False)
    temperature = tk.StringVar(value="—")
    preview_text = tk.StringVar(value="No preview frame")

    outer = ttk.Frame(root, padding=12)
    outer.pack(fill="both", expand=True)
    ttk.Label(outer, text="ToupTek 4040 Camera Diagnostics", font=("", 16, "bold")).pack(anchor="w")
    ttk.Label(outer, text=_OWNER_WARNING, foreground="#a65b00", wraplength=720).pack(anchor="w", pady=(4, 10))
    ttk.Label(outer, textvariable=identity).pack(anchor="w")
    ttk.Label(outer, textvariable=status).pack(anchor="w", pady=(2, 8))

    controls = ttk.LabelFrame(outer, text="Camera controls", padding=10)
    controls.pack(fill="x")
    fields = [
        ("RAW mode", mode, ("RAW 12-bit", "RAW 8-bit")),
        ("Gain", gain, None),
        ("Binning", binning, tuple(str(value) for value in range(1, 9))),
        ("Exposure (s)", duration, None),
        ("ROI X", roi_x, None),
        ("ROI Y", roi_y, None),
        ("ROI width", roi_w, None),
        ("ROI height", roi_h, None),
    ]
    for index, (label, variable, values) in enumerate(fields):
        row, column = divmod(index, 4)
        ttk.Label(controls, text=label).grid(row=row * 2, column=column, sticky="w", padx=4)
        widget = (
            ttk.Combobox(controls, textvariable=variable, values=values, state="readonly", width=15)
            if values
            else ttk.Entry(controls, textvariable=variable, width=17)
        )
        widget.grid(row=row * 2 + 1, column=column, padx=4, pady=(0, 8), sticky="ew")

    cooling = ttk.Frame(outer)
    cooling.pack(fill="x", pady=8)
    ttk.Label(cooling, text="Sensor temperature").pack(side="left")
    ttk.Label(cooling, textvariable=temperature, width=9).pack(side="left")
    ttk.Label(cooling, text="Target °C").pack(side="left", padx=(12, 2))
    ttk.Entry(cooling, textvariable=target, width=8).pack(side="left")
    ttk.Checkbutton(cooling, text="Cooler", variable=cooler).pack(side="left", padx=8)

    buttons = ttk.Frame(outer)
    buttons.pack(fill="x")

    log = tk.Text(outer, height=12, state="disabled", wrap="word")
    log.pack(fill="both", expand=True, pady=(10, 0))
    ttk.Label(outer, textvariable=preview_text).pack(anchor="w", pady=(6, 0))

    def append(message: str) -> None:
        log.configure(state="normal")
        log.insert("end", f"{datetime.now():%H:%M:%S}  {message}\n")
        log.see("end")
        log.configure(state="disabled")
        _LOG.info(message)

    def background(label: str, action) -> None:
        def worker() -> None:
            try:
                result = action()
                events.put(("ok", (label, result)))
            except BaseException as error:
                events.put(("error", (label, error)))

        threading.Thread(target=worker, name=f"diagnostic-{label}", daemon=True).start()

    def settings() -> DiagnosticSettings:
        return DiagnosticSettings(
            duration=float(duration.get()),
            gain=int(gain.get()),
            binning=int(binning.get()),
            mode=ReadoutMode(mode.get()),
            roi=Roi(int(roi_x.get()), int(roi_y.get()), int(roi_w.get()), int(roi_h.get())),
        )

    def connect() -> None:
        background("Connect", lambda: coordinator.set_connected_sync(True))

    def disconnect() -> None:
        background("Disconnect", lambda: coordinator.set_connected_sync(False))

    def expose() -> None:
        selected = settings()
        def start() -> None:
            coordinator.start_exposure(
                selected.duration,
                True,
                settings=lambda _caps: ExposureSettings(
                    selected.roi,
                    selected.binning,
                    selected.mode,
                    selected.gain,
                ),
            )
        background("Exposure", start)

    def toggle_preview(enabled: bool) -> None:
        if enabled:
            background(
                "Preview",
                lambda: coordinator.start_preview(
                    lambda frame: events.put(("preview", frame))
                ),
            )
        else:
            background("Stop preview", coordinator.stop_preview)

    def apply_cooling() -> None:
        value, enabled = float(target.get()), cooler.get()
        def apply() -> None:
            coordinator.set_target_temperature_if_idle(value)
            coordinator.set_cooler_on_if_idle(enabled)
        background("Cooling", apply)

    ttk.Button(buttons, text="Connect", command=connect).pack(side="left")
    ttk.Button(buttons, text="Disconnect", command=disconnect).pack(side="left", padx=4)
    ttk.Button(buttons, text="One exposure", command=expose).pack(side="left", padx=4)
    ttk.Button(buttons, text="Start preview", command=lambda: toggle_preview(True)).pack(side="left", padx=4)
    ttk.Button(buttons, text="Stop preview", command=lambda: toggle_preview(False)).pack(side="left", padx=4)
    ttk.Button(buttons, text="Apply cooling", command=apply_cooling).pack(side="left", padx=4)
    ttk.Button(buttons, text="Abort", command=lambda: background("Abort", coordinator.abort_exposure)).pack(side="right")

    def poll() -> None:
        while True:
            try:
                kind, payload = events.get_nowait()
            except queue.Empty:
                break
            if kind == "error":
                label, error = payload
                text = safe_exception_text(error)
                append(f"{label} failed: {text}")
                if label == "Connect":
                    messagebox.showerror("Camera unavailable", f"{text}\n\n{_OWNER_WARNING}")
            elif kind == "preview":
                frame = payload
                preview_text.set(
                    f"Preview: {frame.pixels.shape[1]} × {frame.pixels.shape[0]}, "
                    f"range {int(frame.pixels.min())}–{int(frame.pixels.max())}"
                )
            else:
                label, _result = payload
                append(f"{label} accepted")
        snapshot = coordinator.snapshot()
        status.set(
            "Connected" if snapshot.connected else
            "Connecting…" if snapshot.connecting else
            f"Error: {safe_exception_text(snapshot.error)}" if snapshot.error else
            "Not connected"
        )
        if snapshot.connected:
            try:
                temperature.set(f"{coordinator.temperature_c:.1f} °C")
            except CameraError:
                temperature.set("Unavailable")
        root.after(200, poll)

    def close() -> None:
        try:
            coordinator.set_connected_sync(False)
        except CameraError:
            _LOG.exception("Diagnostic camera cleanup failed")
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    append("Diagnostics ready; camera remains unopened until Connect")
    poll()
    root.mainloop()
    return 0
