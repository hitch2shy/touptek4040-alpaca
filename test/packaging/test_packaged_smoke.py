from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = Path(
    os.environ.get("TOUPTEK4040_TEST_DIST", ROOT / "dist" / "ToupTek4040-Test")
).resolve()


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.bind(("", 0))
        return int(listener.getsockname()[1])


@dataclass
class PackageServer:
    process: subprocess.Popen[bytes]
    base_url: str

    def request(
        self,
        path: str,
        *,
        body: dict[str, object] | None = None,
    ) -> tuple[int, bytes, str]:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {}
        method = "GET"
        if payload is not None:
            method = "POST"
            headers = {
                "Content-Type": "application/json",
                "Origin": self.base_url,
                "X-ToupTek-Console": "1",
            }
        request = urllib.request.Request(
            self.base_url + path,
            data=payload,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read(), response.headers.get_content_type()

    def json(
        self,
        path: str,
        *,
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        status, payload, _content_type = self.request(path, body=body)
        assert status in {200, 202}
        return json.loads(payload)

    def wait_state(self, predicate, timeout_s: float = 10.0) -> dict[str, object]:
        deadline = time.monotonic() + timeout_s
        last: dict[str, object] = {}
        while time.monotonic() < deadline:
            last = self.json("/web/v1/state")
            if predicate(last):
                return last
            time.sleep(0.05)
        raise AssertionError(f"packaged state did not converge: {last!r}")


def _stop_exact_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT)
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture(scope="module")
def package_server(tmp_path_factory: pytest.TempPathFactory):
    executable = DIST_DIR / "ToupTek4040Alpaca.exe"
    assert executable.is_file(), f"build the temporary package first: {executable}"
    port = _free_tcp_port()
    discovery_port = _free_udp_port()
    log_directory = tmp_path_factory.mktemp("packaged-logs")
    started_at = time.monotonic()
    process = subprocess.Popen(
        [
            str(executable),
            "--source",
            "sim",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--discovery-port",
            str(discovery_port),
            "--log-directory",
            str(log_directory),
        ],
        cwd=DIST_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.CREATE_NO_WINDOW,
    )
    server = PackageServer(process, f"http://127.0.0.1:{port}")
    deadline = time.monotonic() + 20
    last_error: BaseException | None = None
    while time.monotonic() < deadline and process.poll() is None:
        try:
            if server.request("/management/v1/description")[0] == 200:
                break
        except (OSError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(0.1)
    else:
        _stop_exact_process(process)
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(
            f"packaged Alpaca server did not start; pid={process.pid}, "
            f"http_port={port}, discovery_port={discovery_port}, "
            f"elapsed_s={time.monotonic() - started_at:.3f}, "
            f"exit={process.returncode}, last_error={last_error!r}, "
            f"stdout={stdout.decode(errors='replace')!r}, "
            f"stderr={stderr.decode(errors='replace')!r}"
        )
    try:
        yield server
    finally:
        try:
            if process.poll() is None:
                server.request("/web/v1/connection", body={"connected": False})
        except BaseException:
            pass
        _stop_exact_process(process)
        assert process.poll() is not None


def test_packaged_discovery_port_is_probed_with_udp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def bind(self, address: tuple[str, int]) -> None:
            assert address == ("", 0)

        def getsockname(self) -> tuple[str, int]:
            return "0.0.0.0", 54321

    def open_probe(family: int, socket_type: int) -> Probe:
        calls.append((family, socket_type))
        return Probe()

    monkeypatch.setattr(socket, "socket", open_probe)

    assert _free_udp_port() == 54321
    assert calls == [(socket.AF_INET, socket.SOCK_DGRAM)]


def test_packaged_discovery_port_skips_occupied_udp_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied_udp:
        occupied_udp.bind(("", 0))
        occupied_port = int(occupied_udp.getsockname()[1])
        selected = _free_udp_port()
        assert selected != occupied_port
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as verification:
            verification.bind(("", selected))


def test_packaged_alpaca_simulator_serves_management_and_ui(
    package_server: PackageServer,
) -> None:
    assert package_server.request("/management/v1/description")[0] == 200
    status, payload, content_type = package_server.request("/ui/")
    assert status == 200
    assert content_type == "text/html"
    assert b"ToupTek" in payload


def test_packaged_simulator_connects_and_returns_one_image(
    package_server: PackageServer,
) -> None:
    package_server.json("/web/v1/connection", body={"connected": True})
    package_server.wait_state(lambda state: state.get("connected") is True)
    package_server.json(
        "/web/v1/settings",
        body={
            "binning": 1,
            "readoutMode": "RAW 12-bit",
            "gain": 100,
            "roi": {"x": 0, "y": 0, "width": 32, "height": 32},
        },
    )
    package_server.json("/web/v1/exposure", body={"duration": 0.01})
    state = package_server.wait_state(
        lambda current: isinstance(current.get("image"), dict),
    )
    image = state["image"]
    assert isinstance(image, dict)
    assert (image["width"], image["height"]) == (32, 32)
    generation = state["generation"]
    assert isinstance(generation, int)
    status, payload, content_type = package_server.request(
        f"/web/v1/viewport.png?generation={generation}"
    )
    assert status == 200
    assert content_type == "image/png"
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")


def test_packaged_gui_simulator_starts_without_immediate_failure(
    tmp_path: Path,
) -> None:
    executable = DIST_DIR / "ToupTek4040GUI.exe"
    assert executable.is_file(), f"build the temporary package first: {executable}"
    process = subprocess.Popen(
        [
            str(executable),
            "--source",
            "sim",
            "--log-directory",
            str(tmp_path / "gui-logs"),
        ],
        cwd=DIST_DIR,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        time.sleep(1.5)
        assert process.poll() is None
    finally:
        _stop_exact_process(process)
