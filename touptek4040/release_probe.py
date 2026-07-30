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


def _probe_child(connection, generation: int) -> None:
    segment = shared_memory.SharedMemory(
        name=f"touptek4040_probe_{os.getpid()}_{secrets.token_hex(8)}",
        create=True,
        size=len(_PAYLOAD),
    )
    try:
        segment.buf[: len(_PAYLOAD)] = _PAYLOAD
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
        connection.close()


def run_release_owner_probe(result_path: Path) -> int:
    """Run one real spawned child and record a machine-checkable result."""

    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=True)
    process = context.Process(
        target=_probe_child,
        args=(child_connection, 1),
        name="ToupTek4040ReleaseProbeOwner",
    )
    process.start()
    child_connection.close()
    descriptor: dict[str, object] | None = None
    payload_round_trip = False
    ack_count = 0
    segment_name = ""
    try:
        if not parent_connection.poll(10.0):
            raise TimeoutError("release probe descriptor timed out")
        descriptor = parent_connection.recv()
        segment_name = str(descriptor["name"])
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

    live_segment_names: list[str] = []
    if segment_name:
        deadline = time.monotonic() + 2.0
        while True:
            try:
                leftover = shared_memory.SharedMemory(
                    name=segment_name,
                    create=False,
                )
            except FileNotFoundError:
                break
            else:
                leftover.close()
                if time.monotonic() >= deadline:
                    live_segment_names.append(segment_name)
                    break
                time.sleep(0.02)

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
        "registry_names": [],
        "live_segment_names": live_segment_names,
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
            not result["live_segment_names"],
        )
    ) else 1
