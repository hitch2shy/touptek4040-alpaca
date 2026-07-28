from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from test.fixtures.reference_pixels import reference_frame
from touptek4040.image_workspace import ImageWorkspace


def test_published_generation_owns_an_independent_read_only_pixel_snapshot():
    frame = reference_frame()
    workspace = ImageWorkspace()

    generation = workspace.publish(frame)
    frame.pixels[0, 0] = 4095

    assert int(generation.pixels[0, 0]) == 1
    assert generation.frame.pixels is generation.pixels
    assert generation.pixels.flags.writeable is False
    with pytest.raises(ValueError):
        generation.pixels[0, 0] = 2
    with pytest.raises(ValueError):
        generation.pixels.setflags(write=True)
    with pytest.raises(FrozenInstanceError):
        generation.generation_id = 99


def test_formal_and_preview_generations_use_independent_slots():
    workspace = ImageWorkspace()
    formal = workspace.publish(reference_frame())
    preview_frame = reference_frame()
    preview_frame.pixels[:] = 99

    preview = workspace.publish_preview(preview_frame)

    assert workspace.current() is formal
    assert workspace.current_preview() is preview
    assert formal.generation_id < preview.generation_id
    assert int(workspace.current().pixels[0, 0]) == 1
    assert int(workspace.current_preview().pixels[0, 0]) == 99

    workspace.clear_preview()
    assert workspace.current() is formal
    assert workspace.current_preview() is None

    workspace.clear_formal()
    assert workspace.current() is None


def test_concurrent_publications_have_unique_monotonic_generations():
    workspace = ImageWorkspace()

    with ThreadPoolExecutor(max_workers=12) as executor:
        generations = list(
            executor.map(lambda _: workspace.publish(reference_frame()), range(200))
        )

    identifiers = [generation.generation_id for generation in generations]
    assert len(set(identifiers)) == 200
    assert set(identifiers) == set(range(1, 201))
    assert workspace.current().generation_id == 200


@pytest.mark.parametrize(
    ("pixels", "message"),
    [
        (np.zeros((2, 3, 1), dtype=np.uint16), "two-dimensional"),
        (np.zeros((2, 3), dtype=np.int16), "unsigned"),
        (np.zeros((3, 2), dtype=np.uint16), "ROI"),
    ],
)
def test_publish_rejects_frames_that_cannot_be_scientific_image_generations(
    pixels,
    message,
):
    frame = reference_frame()
    object.__setattr__(frame, "pixels", pixels)

    with pytest.raises(ValueError, match=message):
        ImageWorkspace().publish(frame)
