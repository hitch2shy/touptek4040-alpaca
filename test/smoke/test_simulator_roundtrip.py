from __future__ import annotations

import http.client
import json
import struct
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

import run_alpaca
from touptek4040.alpaca.management import stable_unique_id
from touptek4040.config import AppConfig, CameraConfig, LoggingConfig, ServerConfig, WebConfig


def test_complete_simulator_http_roundtrip() -> None:
    config = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=0, discovery_port=32227),
        camera=CameraConfig(source="sim"),
        web=WebConfig(enabled=True),
        logging=LoggingConfig(level="INFO", directory=Path("test/results/logs")),
        config_path=None,
    )
    server = run_alpaca.build_server(config, start_discovery=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = _Client(server.port)
    try:
        versions = base.get_json("/management/apiversions?ClientTransactionID=41")
        assert versions["Value"] == [1]
        assert versions["ClientTransactionID"] == 41
        assert isinstance(versions["ServerTransactionID"], int)
        devices = base.get_json("/management/v1/configureddevices")["Value"]
        assert devices[0]["DeviceType"] == "Camera"
        assert devices[0]["UniqueID"] == stable_unique_id(None, "sim")

        assert base.put_alpaca("connected", Connected="true")["ErrorNumber"] == 0
        _wait_until(lambda: base.get_alpaca("connected")["Value"] is True)
        for command, name, value in (
            ("binx", "BinX", "2"),
            ("biny", "BinY", "2"),
            ("numx", "NumX", "32"),
            ("numy", "NumY", "24"),
            ("gain", "Gain", "250"),
            ("readoutmode", "ReadoutMode", "0"),
        ):
            assert base.put_alpaca(command, **{name: value})["ErrorNumber"] == 0

        assert base.put_alpaca("startexposure", Duration="0.001", Light="true")["ErrorNumber"] == 0
        _wait_until(lambda: base.get_alpaca("imageready")["Value"] is True)

        binary_status, binary_headers, binary = base.request(
            "GET",
            "/api/v1/camera/0/imagearray",
            headers={"Accept": "application/imagebytes"},
        )
        assert binary_status == 200
        assert binary_headers["content-type"].startswith("application/imagebytes")
        metadata = struct.unpack_from("<iiIIiiiiiii", binary)
        assert metadata[0:2] == (1, 0)
        assert metadata[2] == 0
        assert metadata[3] > 0
        assert metadata[4:] == (44, 8, 8, 2, 32, 24, 0)
        assert len(binary) == 44 + 32 * 24 * 2

        image_json = base.get_alpaca("imagearray")
        assert image_json["ErrorNumber"] == 0
        assert image_json["ClientTransactionID"] == base.transaction
        assert image_json["ServerTransactionID"] > 0
        assert len(image_json["Value"]) == 32
        assert len(image_json["Value"][0]) == 24

        state = base.get_json("/web/v1/state")
        generation = state["generation"]
        assert isinstance(generation, int)
        status, headers, png = base.request(
            "GET",
            f"/web/v1/viewport.png?generation={generation}&maxWidth=64&maxHeight=64",
        )
        assert status == 200 and headers["content-type"].startswith("image/png")
        assert headers["x-content-type-options"] == "nosniff"
        assert "default-src 'self'" in headers["content-security-policy"]
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II", png[16:24]) == (32, 24)

        status, headers, fits = base.request(
            "GET", f"/web/v1/image.fits?generation={generation}"
        )
        assert status == 200 and "fits" in headers["content-type"]
        assert fits.startswith(b"SIMPLE")
        assert len(fits) % 2880 == 0
        header_end = next(
            offset + 80
            for offset in range(0, len(fits), 80)
            if fits[offset : offset + 8].strip() == b"END"
        )
        data_start = ((header_end + 2879) // 2880) * 2880
        cards = [fits[offset : offset + 80] for offset in range(0, header_end, 80)]
        assert any(card.startswith(b"NAXIS1  =") and b"32" in card for card in cards)
        assert any(card.startswith(b"NAXIS2  =") and b"24" in card for card in cards)
        assert len(fits[data_start : data_start + 32 * 24 * 2]) == 32 * 24 * 2

        assert base.put_alpaca("startexposure", Duration="0.001", Light="true")["ErrorNumber"] == 0
        _wait_until(lambda: base.get_alpaca("imageready")["Value"] is True)
        current_generation = base.get_json("/web/v1/state")["generation"]
        assert current_generation > generation
        stale_status, _stale_headers, _stale = base.request(
            "GET", f"/web/v1/viewport.png?generation={generation}"
        )
        assert stale_status == 409
        current_status, _current_headers, _current = base.request(
            "GET", f"/web/v1/viewport.png?generation={current_generation}"
        )
        assert current_status == 200

        assert base.put_alpaca("startexposure", Duration="1", Light="true")["ErrorNumber"] == 0
        assert base.put_alpaca("abortexposure")["ErrorNumber"] == 0
        _wait_until(lambda: base.get_alpaca("camerastate")["Value"] == 0)

        assert base.put_alpaca("connected", Connected="false")["ErrorNumber"] == 0
        _wait_until(lambda: base.get_alpaca("connected")["Value"] is False)
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert not thread.is_alive()


class _Client:
    def __init__(self, port: int) -> None:
        self.port = port
        self.transaction = 0

    def request(self, method: str, path: str, body: bytes | None = None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = response.read()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, payload
        finally:
            connection.close()

    def get_json(self, path: str):
        status, _headers, body = self.request("GET", path)
        assert status == 200
        return json.loads(body)

    def get_alpaca(self, command: str):
        self.transaction += 1
        return self.get_json(
            f"/api/v1/camera/0/{command}?ClientID=1&ClientTransactionID={self.transaction}"
        )

    def put_alpaca(self, command: str, **values: str):
        self.transaction += 1
        fields = {
            "ClientID": "1",
            "ClientTransactionID": str(self.transaction),
            **values,
        }
        body = urlencode(fields).encode("ascii")
        status, _headers, payload = self.request(
            "PUT",
            f"/api/v1/camera/0/{command}",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert status == 200
        return json.loads(payload)


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("timed out waiting for simulator state")
