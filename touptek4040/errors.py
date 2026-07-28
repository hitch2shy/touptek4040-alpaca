"""Domain errors raised by camera implementations."""


class CameraError(Exception):
    """Base class for camera-facing failures."""


class CameraBusyError(CameraError):
    """Raised when an operation cannot run while the camera is busy."""


class CameraNotConnectedError(CameraError):
    """Raised when an operation requires an active camera connection."""


class CameraInvalidValueError(CameraError):
    """Raised when a camera operation receives an unsupported value."""
