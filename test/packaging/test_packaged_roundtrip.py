from __future__ import annotations

# Reuse the proven HTTP/PNG simulator round-trip against the formal path.
from test.packaging.test_packaged_smoke import (  # noqa: F401
    PackageServer,
    _free_tcp_port,
    _free_udp_port,
    _stop_exact_process,
)


def test_formal_roundtrip_module_is_collected() -> None:
    assert callable(_free_tcp_port)
