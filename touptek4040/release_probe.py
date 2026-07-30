"""Narrow frozen-runtime probe for Windows spawn and shared memory."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
from multiprocessing import shared_memory
import os
from pathlib import Path
import secrets
import time


_PAYLOAD = b"ToupTek4040 frozen owner shared-memory probe v1"


def read_registry_names(registry_path: Path) -> list[str]:
    """Read the names actually present in one probe registry snapshot."""

    try:
        document = json.loads(registry_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    frames = document.get("frames") if isinstance(document, dict) else None
    if not isinstance(frames, list):
        raise ValueError("release probe registry has an invalid schema")
    names: list[str] = []
    for frame in frames:
        if not isinstance(frame, dict) or not isinstance(frame.get("name"), str):
            raise ValueError("release probe registry has an invalid frame")
        names.append(frame["name"])
    return sorted(set(names))


def probe_live_names(names: list[str]) -> list[str]:
    """Attempt to reopen every observed mapping and return those still live."""

    live: list[str] = []
    for name in sorted(set(names)):
        try:
            mapping = shared_memory.SharedMemory(name=name, create=False)
        except FileNotFoundError:
            continue
        else:
            mapping.close()
            live.append(name)
    return live


def _write_registry(registry_path: Path, names: list[str]) -> None:
    temporary = registry_path.with_name(registry_path.name + ".tmp")
    temporary.write_text(
        json.dumps({"frames": [{"name": name} for name in names]}) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, registry_path)


def _probe_child(connection, generation: int, registry_path: str) -> None:
    registry = Path(registry_path)
    segment = shared_memory.SharedMemory(
        name=f"touptek4040_probe_{os.getpid()}_{secrets.token_hex(8)}",
        create=True,
        size=len(_PAYLOAD),
    )
    try:
        segment.buf[: len(_PAYLOAD)] = _PAYLOAD
        _write_registry(registry, [segment.name])
        connection.send(
            {
                "child_pid": os.getpid(),
                "generation": generation,
                "publication_count": 1,
                "name": segment.name,
                "size": len(_PAYLOAD),
                "sha256": hashlib.sha256(_PAYLOAD).hexdigest(),
            }
        )
        if not connection.poll(10.0) or connection.recv() != {"ack": 1}:
            raise RuntimeError("release probe did not receive the exact ACK")
        connection.send({"ack_count": 1})
    finally:
        segment.close()
        segment.unlink()
        _write_registry(registry, [])
        connection.close()


def run_release_owner_probe(result_path: Path) -> int:
    """Run one real spawned child and record a machine-checkable result."""

    context = multiprocessing.get_context("spawn")
    registry_path = result_path.with_name(result_path.stem + "-registry.json")
    parent_connection, child_connection = context.Pipe(duplex=True)
    process = context.Process(
        target=_probe_child,
        args=(child_connection, 1, str(registry_path)),
        name="ToupTek4040ReleaseProbeOwner",
    )
    process.start()
    child_connection.close()
    descriptor: dict[str, object] | None = None
    payload_round_trip = False
    ack_count = 0
    segment_name = ""
    observed_names: list[str] = []
    try:
        if not parent_connection.poll(10.0):
            raise TimeoutError("release probe descriptor timed out")
        descriptor = parent_connection.recv()
        segment_name = str(descriptor["name"])
        observed_names = sorted(
            set(read_registry_names(registry_path) + [segment_name])
        )
        segment = shared_memory.SharedMemory(name=segment_name, create=False)
        try:
            payload = bytes(segment.buf[: int(descriptor["size"])])
        finally:
            segment.close()
        payload_round_trip = (
            payload == _PAYLOAD
            and hashlib.sha256(payload).hexdigest() == descriptor["sha256"]
        )
        parent_connection.send({"ack": 1})
        if not parent_connection.poll(10.0):
            raise TimeoutError("release probe ACK confirmation timed out")
        ack_count = int(parent_connection.recv()["ack_count"])
    finally:
        parent_connection.close()
        process.join(10.0)
        if process.is_alive():
            process.terminate()
            process.join(5.0)

    registry_names = read_registry_names(registry_path)
    probed_names = list(observed_names)
    live_names = probe_live_names(probed_names)
    try:
        registry_path.unlink()
    except FileNotFoundError:
        pass

    result = {
        "parent_pid": os.getpid(),
        "child_pid": None if descriptor is None else descriptor["child_pid"],
        "generation": None if descriptor is None else descriptor["generation"],
        "publication_count": (
            0 if descriptor is None else descriptor["publication_count"]
        ),
        "ack_count": ack_count,
        "payload_round_trip": payload_round_trip,
        "child_dead": not process.is_alive() and process.exitcode == 0,
        "descriptor_name": segment_name,
        "observed_names": observed_names,
        "probed_names": probed_names,
        "registry_names": registry_names,
        "live_names": live_names,
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = result_path.with_name(result_path.name + ".tmp")
    temporary.write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, result_path)
    return 0 if all(
        (
            result["payload_round_trip"],
            result["child_dead"],
            result["publication_count"] == 1,
            result["ack_count"] == 1,
            result["observed_names"] == [result["descriptor_name"]],
            result["probed_names"] == result["observed_names"],
            not result["registry_names"],
            not result["live_names"],
        )
    ) else 1
