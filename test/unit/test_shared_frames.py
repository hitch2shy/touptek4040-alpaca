"""Integration tests for real owner-process shared-memory frames."""

from __future__ import annotations

from dataclasses import replace
import json
from multiprocessing import get_context, shared_memory
from pathlib import Path
import time
import tracemalloc

import numpy as np
import pytest

from touptek4040.owner_protocol import FrameDescriptor, OwnerProtocolError, PROTOCOL_VERSION
from touptek4040.shared_frames import SharedFrameError, SharedFrameRegistry


def _registry(tmp_path: Path) -> SharedFrameRegistry:
    return SharedFrameRegistry(owner_generation=7, registry_path=tmp_path / "frames.json")


def _mapping_exists(name: str) -> bool:
    try:
        mapping = shared_memory.SharedMemory(name=name)
    except FileNotFoundError:
        return False
    else:
        mapping.close()
        return True


def _child_publish_and_hold(
    registry_path: str,
    result_queue: object,
    release_event: object,
    exit_event: object,
    shape: tuple[int, int],
    dtype_name: str,
) -> None:
    """Spawn target: publish a real frame and retain its owner handle."""
    registry = SharedFrameRegistry(owner_generation=7, registry_path=registry_path)
    dtype = np.dtype(dtype_name)
    max_adu = 255 if dtype == np.dtype("uint8") else 4095
    pixel_format = "RAW8" if dtype == np.dtype("uint8") else "RAW12"
    descriptor = registry.publish(
        np.full(shape, max_adu, dtype=dtype),
        {"pixel_format": pixel_format, "max_adu": max_adu},
    )
    result_queue.put(("published", descriptor))  # type: ignore[attr-defined]
    if release_event.wait(30):  # type: ignore[attr-defined]
        registry.release(descriptor)
        result_queue.put(("released", descriptor.name))  # type: ignore[attr-defined]
    exit_event.wait(30)  # type: ignore[attr-defined]


def _child_publish_then_exit(registry_path: str, result_queue: object) -> None:
    """Spawn target: leave a real registry entry behind as a crashed owner would."""
    registry = SharedFrameRegistry(owner_generation=7, registry_path=registry_path)
    descriptor = registry.publish(
        np.full((2, 2), 255, dtype=np.uint8),
        {"pixel_format": "RAW8", "max_adu": 255},
    )
    result_queue.put(descriptor)  # type: ignore[attr-defined]


def _child_publish_many_and_hold(
    registry_path: str,
    ready_queue: object,
    result_queue: object,
    start_event: object,
    release_event: object,
    count: int,
) -> None:
    """Spawn target: force real concurrent registry read-modify-replace work."""
    registry = SharedFrameRegistry(owner_generation=7, registry_path=registry_path)
    ready_queue.put("ready")  # type: ignore[attr-defined]
    start_event.wait(30)  # type: ignore[attr-defined]
    descriptors = [
        registry.publish(
            np.full((2, 2), index, dtype=np.uint8),
            {"pixel_format": "RAW8", "max_adu": 255},
        )
        for index in range(count)
    ]
    result_queue.put([descriptor.name for descriptor in descriptors])  # type: ignore[attr-defined]
    release_event.wait(30)  # type: ignore[attr-defined]
    for descriptor in descriptors:
        registry.release(descriptor)


def _start_child_publisher(tmp_path: Path, shape: tuple[int, int], dtype_name: str):
    context = get_context("spawn")
    result_queue = context.Queue()
    release_event = context.Event()
    exit_event = context.Event()
    process = context.Process(
        target=_child_publish_and_hold,
        args=(str(tmp_path / "frames.json"), result_queue, release_event, exit_event, shape, dtype_name),
    )
    process.start()
    kind, descriptor = result_queue.get(timeout=30)
    assert kind == "published"
    assert isinstance(descriptor, FrameDescriptor)
    return process, result_queue, release_event, exit_event, descriptor


def _stop_child_publisher(process: object, release_event: object, exit_event: object) -> None:
    release_event.set()  # type: ignore[attr-defined]
    exit_event.set()  # type: ignore[attr-defined]
    process.join(30)  # type: ignore[attr-defined]
    if process.is_alive():  # type: ignore[attr-defined]
        process.terminate()  # type: ignore[attr-defined]
        process.join(30)  # type: ignore[attr-defined]


def test_raw8_publish_consume_copies_pixels_makes_them_read_only_and_releases_mapping(tmp_path: Path):
    """Returning a shared, mutable, or unlinked-too-late RAW8 frame is a bug."""
    registry = _registry(tmp_path)
    pixels = np.arange(12, dtype=np.uint8).reshape(3, 4)

    descriptor = registry.publish(pixels, {"pixel_format": "RAW8", "max_adu": 255, "tag": "guide"})
    received = registry.consume(descriptor)

    assert descriptor.name.startswith("touptek4040-g7-r1-")
    assert descriptor.nbytes == 12
    assert np.array_equal(received, pixels)
    assert received is not pixels
    assert received.flags.writeable is False
    assert _mapping_exists(descriptor.name) is True
    registry.release(descriptor)
    assert _mapping_exists(descriptor.name) is False


def test_raw12_round_trip_preserves_uint16_values_and_metadata(tmp_path: Path):
    """Converting RAW12 values or losing their frame metadata is a bug."""
    registry = _registry(tmp_path)
    pixels = np.array([[0, 1, 2048], [4094, 4095, 17]], dtype=np.uint16)

    descriptor = registry.publish(
        pixels,
        {"pixel_format": "RAW12", "max_adu": 4095, "gain": 100, "cooler": None},
    )
    received = registry.consume(descriptor)

    assert descriptor.dtype == "uint16"
    assert descriptor.max_adu == 4095
    assert descriptor.metadata == {"pixel_format": "RAW12", "max_adu": 4095, "gain": 100, "cooler": None}
    assert received.dtype == np.dtype("uint16")
    assert np.array_equal(received, pixels)
    assert received.flags.writeable is False
    registry.release(descriptor)


def test_parent_consumes_a_frame_published_by_a_real_windows_child_process(tmp_path: Path):
    """Rebuilding a child publisher record with the parent PID is a bug."""
    process, _queue, release_event, exit_event, descriptor = _start_child_publisher(tmp_path, (2, 3), "uint8")
    try:
        received = SharedFrameRegistry(owner_generation=7, registry_path=tmp_path / "frames.json").consume(descriptor)

        assert np.array_equal(received, np.full((2, 3), 255, dtype=np.uint8))
        assert received.flags.writeable is False
    finally:
        _stop_child_publisher(process, release_event, exit_event)


def test_consumer_defers_32mib_mapping_release_to_the_real_publisher(tmp_path: Path):
    """A consumer must not claim it released the publisher's Windows handle."""
    process, result_queue, release_event, exit_event, descriptor = _start_child_publisher(
        tmp_path, (4096, 4096), "uint16"
    )
    try:
        received = SharedFrameRegistry(owner_generation=7, registry_path=tmp_path / "frames.json").consume(descriptor)

        assert received.nbytes == 4096 * 4096 * 2
        assert _mapping_exists(descriptor.name) is True

        release_event.set()
        kind, name = result_queue.get(timeout=30)
        assert (kind, name) == ("released", descriptor.name)
        assert _mapping_exists(descriptor.name) is False
        assert json.loads((tmp_path / "frames.json").read_text(encoding="utf-8")) == {"frames": []}
    finally:
        _stop_child_publisher(process, release_event, exit_event)


def test_startup_cleans_a_registry_entry_left_by_a_real_exited_child_owner(tmp_path: Path):
    """Mistaking an exited Windows child PID for live leaves its registry entry behind."""
    context = get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(target=_child_publish_then_exit, args=(str(tmp_path / "frames.json"), result_queue))
    process.start()
    descriptor = result_queue.get(timeout=30)
    result_queue.close()
    result_queue.join_thread()
    process.join(30)
    assert process.exitcode == 0
    process.close()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        SharedFrameRegistry(owner_generation=8, registry_path=tmp_path / "frames.json")
        if json.loads((tmp_path / "frames.json").read_text(encoding="utf-8")) == {"frames": []}:
            break
        time.sleep(0.01)

    assert _mapping_exists(descriptor.name) is False
    assert json.loads((tmp_path / "frames.json").read_text(encoding="utf-8")) == {"frames": []}


def test_concurrent_child_publishers_do_not_lose_atomic_registry_entries(tmp_path: Path):
    """A process-local RLock lets concurrent publishers overwrite each other's records."""
    context = get_context("spawn")
    ready_queue = context.Queue()
    result_queue = context.Queue()
    start_event = context.Event()
    release_event = context.Event()
    processes = [
        context.Process(
            target=_child_publish_many_and_hold,
            args=(str(tmp_path / "frames.json"), ready_queue, result_queue, start_event, release_event, 8),
        )
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    try:
        assert [ready_queue.get(timeout=30) for _ in processes] == ["ready"] * 4
        start_event.set()
        names = {name for _ in processes for name in result_queue.get(timeout=30)}

        recorded = json.loads((tmp_path / "frames.json").read_text(encoding="utf-8"))
        assert {entry["name"] for entry in recorded["frames"]} == names
        assert len(names) == 32
    finally:
        release_event.set()
        for process in processes:
            process.join(30)
            if process.is_alive():
                process.terminate()
                process.join(30)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda record: record.__setitem__("owner_pid", True),
        lambda record: record.__setitem__("shape", [True, 2]),
        lambda record: record.__setitem__("name", "touptek4040-g999-r1-" + "0" * 32),
        lambda record: record.__setitem__("unexpected", None),
    ],
)
def test_registry_rejects_adversarial_entries_without_cleaning_their_mapping(tmp_path: Path, tamper: object):
    """Bool IDs, inconsistent product names, or unknown fields must not authorize cleanup."""
    registry = _registry(tmp_path)
    descriptor = registry.publish(
        np.full((2, 2), 255, dtype=np.uint8),
        {"pixel_format": "RAW8", "max_adu": 255},
    )
    registry_path = tmp_path / "frames.json"
    original = json.loads(registry_path.read_text(encoding="utf-8"))
    corrupted = json.loads(registry_path.read_text(encoding="utf-8"))
    tamper(corrupted["frames"][0])  # type: ignore[operator]
    registry_path.write_text(json.dumps(corrupted), encoding="utf-8")

    try:
        with pytest.raises(SharedFrameError):
            SharedFrameRegistry(owner_generation=8, registry_path=registry_path)
        assert _mapping_exists(descriptor.name) is True
    finally:
        registry_path.write_text(json.dumps(original), encoding="utf-8")
        registry.release(descriptor)


def test_consume_rejects_a_descriptor_with_wrong_byte_length(tmp_path: Path):
    """Trusting a descriptor whose declared byte length changed is a bug."""
    registry = _registry(tmp_path)
    descriptor = registry.publish(
        np.arange(12, dtype=np.uint8).reshape(3, 4),
        {"pixel_format": "RAW8", "max_adu": 255},
    )

    with pytest.raises(SharedFrameError):
        registry.consume(replace(descriptor, shape=(1, 11), nbytes=11))

    registry.release(descriptor)


def test_consume_rejects_a_descriptor_with_a_wrong_shape_even_when_bytes_match(tmp_path: Path):
    """Reinterpreting bytes as another same-size image shape is a bug."""
    registry = _registry(tmp_path)
    descriptor = registry.publish(
        np.arange(12, dtype=np.uint8).reshape(2, 6),
        {"pixel_format": "RAW8", "max_adu": 255},
    )

    with pytest.raises(SharedFrameError):
        registry.consume(replace(descriptor, shape=(3, 4)))

    registry.release(descriptor)


def test_publish_rejects_unsupported_pixel_dtypes(tmp_path: Path):
    """Publishing float pixels as a camera raw frame is a bug."""
    registry = _registry(tmp_path)

    with pytest.raises(SharedFrameError):
        registry.publish(
            np.zeros((2, 2), dtype=np.float32),
            {"pixel_format": "RAW8", "max_adu": 255},
        )


def test_frame_descriptor_rejects_impossible_dimensions():
    """Accepting zero or overflowing frame dimensions is a bug."""
    with pytest.raises(OwnerProtocolError):
        FrameDescriptor(
            protocol_version=PROTOCOL_VERSION,
            owner_generation=7,
            request_id=1,
            name="touptek4040-g7-r1-impossible",
            shape=(0, 12),
            dtype="uint8",
            nbytes=0,
            max_adu=255,
            metadata={"pixel_format": "RAW8", "max_adu": 255},
        )

    with pytest.raises(OwnerProtocolError):
        FrameDescriptor(
            protocol_version=PROTOCOL_VERSION,
            owner_generation=7,
            request_id=1,
            name="touptek4040-g7-r1-overflow",
            shape=(2**62, 4),
            dtype="uint8",
            nbytes=2**64,
            max_adu=255,
            metadata={"pixel_format": "RAW8", "max_adu": 255},
        )


@pytest.mark.parametrize(
    ("pixels", "metadata"),
    [
        (np.zeros((2, 2), dtype=np.uint8), {"pixel_format": "RAW8", "max_adu": 4095}),
        (np.zeros((2, 2), dtype=np.uint16), {"pixel_format": "RAW12", "max_adu": 4096}),
    ],
)
def test_publish_rejects_metadata_with_a_mismatched_max_adu(
    tmp_path: Path, pixels: np.ndarray, metadata: dict[str, object]
):
    """Letting metadata claim an incompatible raw bit depth is a bug."""
    with pytest.raises(SharedFrameError):
        _registry(tmp_path).publish(pixels, metadata)


def test_owner_crash_before_publication_leaves_no_registry_file(tmp_path: Path):
    """Creating a registry entry before a frame exists would leak after an owner crash."""
    registry_path = tmp_path / "frames.json"
    SharedFrameRegistry(owner_generation=7, registry_path=registry_path)

    assert registry_path.exists() is False


def test_release_is_idempotent_and_removes_the_real_mapping_and_registry_entry(tmp_path: Path):
    """A second cleanup attempt must be harmless after the exact frame is released."""
    registry = _registry(tmp_path)
    descriptor = registry.publish(
        np.arange(4, dtype=np.uint8).reshape(2, 2),
        {"pixel_format": "RAW8", "max_adu": 255},
    )

    registry.release(descriptor)
    registry.release(descriptor)

    assert _mapping_exists(descriptor.name) is False
    assert json.loads((tmp_path / "frames.json").read_text(encoding="utf-8")) == {"frames": []}


def test_registry_file_is_replaced_when_a_new_frame_is_published(tmp_path: Path):
    """Writing the registry in place could leave a crash-truncated registry file."""
    registry = _registry(tmp_path)
    first = registry.publish(np.zeros((1, 1), dtype=np.uint8), {"pixel_format": "RAW8", "max_adu": 255})
    registry_path = tmp_path / "frames.json"
    first_file_id = registry_path.stat().st_ino

    second = registry.publish(np.ones((1, 1), dtype=np.uint8), {"pixel_format": "RAW8", "max_adu": 255})

    recorded = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry_path.stat().st_ino != first_file_id
    assert {entry["name"] for entry in recorded["frames"]} == {first.name, second.name}

    registry.release(first)
    registry.release(second)


def test_raw12_4096_square_copy_has_the_exact_size_and_bounded_python_peak_memory(tmp_path: Path):
    """A full-sensor RAW12 copy must stay a 32 MiB frame rather than accidentally expanding."""
    registry = _registry(tmp_path)
    pixels = np.full((4096, 4096), 4095, dtype=np.uint16)
    tracemalloc.start()
    try:
        descriptor = registry.publish(pixels, {"pixel_format": "RAW12", "max_adu": 4095})
        received = registry.consume(descriptor)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    frame_bytes = 4096 * 4096 * 2
    print(f"RAW12 4096x4096 frame_bytes={frame_bytes} traced_peak_bytes={peak}")
    assert descriptor.nbytes == frame_bytes
    assert received.nbytes == frame_bytes
    assert np.array_equal(received, pixels)
    assert peak <= frame_bytes * 4 + 8 * 1024 * 1024
    registry.release(descriptor)
