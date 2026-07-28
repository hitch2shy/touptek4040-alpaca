"""Domain errors raised by camera implementations."""


class CameraError(Exception):
    """Base class for camera-facing failures."""


class CameraBusyError(CameraError):
    """Raised when an operation cannot run while the camera is busy."""


class CameraNotConnectedError(CameraError):
    """Raised when an operation requires an active camera connection."""


class CameraInvalidValueError(CameraError):
    """Raised when a camera operation receives an unsupported value."""


class CameraNotSupportedError(CameraError):
    """Raised when the attached camera does not implement an operation."""


class CameraTimeoutError(CameraError):
    """Raised when the camera does not deliver a frame before its deadline."""


class CameraOperationCancelledError(CameraError):
    """Raised when an in-flight operation is explicitly cancelled."""


class CameraCleanupError(CameraError):
    """Raised when more than one native cleanup operation fails."""

    def __init__(self, message: str, errors: tuple[CameraError, ...]) -> None:
        super().__init__(message)
        self.errors = errors


_SAFE_EXCEPTION_TEXT_LIMIT = 512


def safe_exception_text(
    error: BaseException,
    *,
    fallback: str = "camera operation failed",
    limit: int = _SAFE_EXCEPTION_TEXT_LIMIT,
    reject_line_breaks: bool = True,
) -> str:
    """Return bounded single-line exception text without invoking repr."""

    safe_fallback = fallback[:limit] if fallback else ""
    try:
        text = str(error)
        if not isinstance(text, str):
            return safe_fallback
        bounded = text[:limit]
        if reject_line_breaks and any(character in bounded for character in "\r\n"):
            return safe_fallback
        single_line = "".join(
            character if character.isprintable() else " "
            for character in bounded
        ).strip()
        return single_line or safe_fallback
    except BaseException:
        return safe_fallback


def safe_exception_identity(error: BaseException) -> str:
    """Return an allow-listed exception identity without dynamic class text."""

    identities = {
        CameraNotConnectedError: "CameraNotConnectedError",
        CameraTimeoutError: "CameraTimeoutError",
        CameraOperationCancelledError: "CameraOperationCancelledError",
        CameraBusyError: "CameraBusyError",
        CameraInvalidValueError: "CameraInvalidValueError",
        CameraNotSupportedError: "CameraNotSupportedError",
        CameraCleanupError: "CameraCleanupError",
        CameraError: "CameraError",
        RuntimeError: "RuntimeError",
        ValueError: "ValueError",
        OSError: "OSError",
    }
    try:
        return identities.get(type(error), "Exception")
    except BaseException:
        return "Exception"


def safe_camera_error(error: BaseException, operation: str) -> CameraError:
    """Normalize any throwable into a safe domain error that can be stringified."""

    fallback = f"{operation} failed"
    text = safe_exception_text(error, fallback=fallback)
    try:
        if isinstance(error, CameraCleanupError):
            nested = tuple(
                safe_camera_error(item, operation)
                for item in error.errors[:32]
            )
            if not nested:
                nested = (CameraError(fallback),)
            return CameraCleanupError(text, nested)
        error_type: type[CameraError]
        if isinstance(error, CameraNotConnectedError):
            error_type = CameraNotConnectedError
        elif isinstance(error, CameraTimeoutError):
            error_type = CameraTimeoutError
        elif isinstance(error, CameraOperationCancelledError):
            error_type = CameraOperationCancelledError
        elif isinstance(error, CameraBusyError):
            error_type = CameraBusyError
        elif isinstance(error, CameraInvalidValueError):
            error_type = CameraInvalidValueError
        elif isinstance(error, CameraNotSupportedError):
            error_type = CameraNotSupportedError
        else:
            error_type = CameraError
        if not isinstance(error, CameraError):
            text = f"{operation} failed: {text}"
        return error_type(text)
    except BaseException:
        return CameraError(fallback)
