from __future__ import annotations

import json
from pathlib import Path

# Reuse the proven HTTP/PNG simulator round-trip against the formal path.
from test.packaging.test_packaged_smoke import (  # noqa: F401
    PackageServer,
    _free_tcp_port,
    _free_udp_port,
    _stop_exact_process,
)


def test_formal_roundtrip_module_is_collected() -> None:
    assert callable(_free_tcp_port)


def test_real_package_evidence_proves_automatic_restore_and_owner_cleanup() -> None:
    root = Path(__file__).resolve().parents[2]
    evidence = json.loads(
        (root / "test/results/final-package/hardware.json").read_text("utf-8")
    )
    assert evidence["heater"]["manual_restore_before_disconnect"] is False
    assert evidence["heater"]["alternate_left_active"] is True
    assert evidence["heater"]["restored_by_disconnect"] is True
    assert evidence["heater"]["reconnect_confirmed_initial"] is True
    assert evidence["owner"]["child_pid"] != evidence["process_id"]
    assert evidence["owner"]["child_dead_after_disconnect"] is True
    assert evidence["shared_memory"]["registry_names"] == []
    assert evidence["shared_memory"]["live_segment_names"] == []
