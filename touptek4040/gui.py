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


_DISPLAY_MODES = {
    "RAW 12-bit": ReadoutMode.RAW12,
    "RAW 8-bit": ReadoutMode.RAW8,
}


def parse_diagnostic_settings(
    *,
    duration: str,
    gain: str,
    binning: str,
    mode: str,
    roi_x: str,
    roi_y: str,
    roi_width: str,
    roi_height: str,
) -> DiagnosticSettings:
    """Translate stable display labels and user fields without leaking parser errors."""

    try:
        readout_mode = _DISPLAY_MODES[mode]
        parsed = DiagnosticSettings(
            duration=float(duration),
            gain=int(gain),
            binning=int(binning),
            mode=readout_mode,
            roi=Roi(
                int(roi_x),
                int(roi_y),
                int(roi_width),
                int(roi_height),
            ),
        )
        if parsed.duration < 0 or parsed.gain < 0 or not 1 <= parsed.binning <= 8:
            raise ValueError
        if parsed.roi.width <= 0 or parsed.roi.height <= 0:
            raise ValueError
        return parsed
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "Check the camera setting fields and try again."
        ) from error


class QueueLogHandler(logging.Handler):
    """Forward formatted package logs to a thread-safe UI queue sink."""

    def __init__(self, sink) -> None:
        super().__init__()
        self._sink = sink
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink(self.format(record))
        except BaseException:
            self.handleError(record)


class DiagnosticWorkerPool:
    """Track diagnostic actions and make close win over queued camera opens."""

    def __init__(self, thread_factory=threading.Thread) -> None:
        self._lock = threading.Lock()
        self._closing = False
        self._workers: set[threading.Thread] = set()
        self._thread_factory = thread_factory
        self._finalizer: threading.Thread | None = None

    @property
    def closing(self) -> bool:
        with self._lock:
            return self._closing

    def submit(self, action, callback) -> bool:
        with self._lock:
            if self._closing:
                return False
            holder = {}

            def run() -> None:
                with self._lock:
                    worker = holder["worker"]
                    if self._closing:
                        self._workers.discard(worker)
                        return
                try:
                    callback("ok", action())
                except BaseException as error:
                    callback("error", error)
                finally:
                    with self._lock:
                        self._workers.discard(holder["worker"])

            worker = self._thread_factory(
                target=run,
                name="diagnostic-action",
                daemon=True,
            )
            holder["worker"] = worker
            self._workers.add(worker)
            worker.start()
            return True

    def close(self, cleanup) -> threading.Thread:
        with self._lock:
            if self._finalizer is not None:
                return self._finalizer
            self._closing = True
            workers = tuple(self._workers)

            def quiesce() -> None:
                for worker in workers:
                    worker.join()
                cleanup()

            self._finalizer = threading.Thread(
                target=quiesce,
                name="diagnostic-final-cleanup",
                daemon=True,
            )
            self._finalizer.start()
            return self._finalizer


def run_mainloop_with_cleanup(root: Any, cleanup) -> None:
    """Guarantee cleanup for normal exit, Tk failures, and KeyboardInterrupt."""

    try:
        root.mainloop()
    finally:
        cleanup()


def run_diagnostic_gui(
    config: AppConfig,
    *,
    _ui_modules=None,
) -> int:
    """Open a compact diagnostics window; no camera is touched before Connect."""

    if _ui_modules is None:
        import tkinter as tk
        from tkinter import messagebox, ttk
    else:
        tk, messagebox, ttk = _ui_modules

    from run_alpaca import build_coordinator, configure_logging

    logging_session = configure_logging(config)
    package_logger = logging.getLogger("touptek4040")
    original_handlers = tuple(package_logger.handlers)
    try:
        coordinator, resolved = build_coordinator(config)
    except BaseException as error:
        logging_session.close()
        messagebox.showerror(
            "ToupTek 4040 diagnostics",
            f"Camera enumeration failed.\n\n{safe_exception_text(error)}\n\n{_OWNER_WARNING}",
        )
        return 2

    cleanup_scheduled = [False]
    try:
        return _run_diagnostic_window(
            coordinator,
            resolved,
            logging_session,
            tk,
            messagebox,
            ttk,
            cleanup_scheduled,
        )
    finally:
        for handler in tuple(package_logger.handlers):
            if handler not in original_handlers and isinstance(
                handler, QueueLogHandler
            ):
                package_logger.removeHandler(handler)
                handler.close()
        if not cleanup_scheduled[0]:
            _cleanup_coordinator(coordinator)
        logging_session.close()


def _run_diagnostic_window(
    coordinator: CameraCoordinator,
    resolved: AppConfig,
    logging_session,
    tk,
    messagebox,
    ttk,
    cleanup_scheduled: list[bool],
) -> int:
    root = tk.Tk()
    root.title("ToupTek 4040 Diagnostics")
    root.minsize(760, 560)
    events: queue.Queue[tuple[str, object]] = queue.Queue()
    workers = DiagnosticWorkerPool()
    package_logger = logging.getLogger("touptek4040")
    queue_handler = QueueLogHandler(
        lambda text: events.put(("log", text))
    )
    package_logger.addHandler(queue_handler)
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

    def background(label: str, action) -> None:
        accepted = workers.submit(
            action,
            lambda kind, payload: events.put((kind, (label, payload))),
        )
        if not accepted:
            append(f"{label} ignored because diagnostics are closing")

    def settings() -> DiagnosticSettings:
        return parse_diagnostic_settings(
            duration=duration.get(),
            gain=gain.get(),
            binning=binning.get(),
            mode=mode.get(),
            roi_x=roi_x.get(),
            roi_y=roi_y.get(),
            roi_width=roi_w.get(),
            roi_height=roi_h.get(),
        )

    def connect() -> None:
        background("Connect", lambda: coordinator.set_connected_sync(True))

    def disconnect() -> None:
        background("Disconnect", lambda: coordinator.set_connected_sync(False))

    def expose() -> None:
        try:
            selected = settings()
        except ValueError as error:
            append(str(error))
            messagebox.showerror("Invalid settings", str(error))
            return
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
        try:
            value, enabled = float(target.get()), cooler.get()
        except ValueError:
            message = "Check the target temperature and try again."
            append(message)
            messagebox.showerror("Invalid settings", message)
            return
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
            elif kind == "log":
                append(str(payload))
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

    closed = False

    def camera_cleanup() -> None:
        _cleanup_coordinator(coordinator)

    def close() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        for child in buttons.winfo_children():
            child.configure(state="disabled")
        workers.close(camera_cleanup)
        cleanup_scheduled[0] = True
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    append("Diagnostics ready; camera remains unopened until Connect")
    poll()
    def final_cleanup() -> None:
        if not closed:
            workers.close(camera_cleanup)
            cleanup_scheduled[0] = True
        package_logger.removeHandler(queue_handler)
        queue_handler.close()
        logging_session.close()
    run_mainloop_with_cleanup(root, final_cleanup)
    return 0


def _cleanup_coordinator(coordinator: CameraCoordinator) -> None:
    operations = (
        lambda: coordinator.stop_preview() if coordinator.preview_active else None,
        coordinator.abort_exposure,
        lambda: coordinator.set_connected_sync(False),
    )
    for operation in operations:
        try:
            operation()
        except CameraError:
            _LOG.exception("Diagnostic camera cleanup step failed")
        except BaseException:
            _LOG.exception("Unexpected diagnostic camera cleanup failure")
