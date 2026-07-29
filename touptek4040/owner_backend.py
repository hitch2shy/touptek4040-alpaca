"""The narrow backend surface owned exclusively by the camera process."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .camera_contract import CameraContract


@runtime_checkable
class OwnerBackend(CameraContract, Protocol):
    """Camera operations the isolated owner may dispatch explicitly."""
