"""Small ToupCam test double reserved for hardware-adapter tests."""


class FakeToupCam:
    """Minimal explicit fake; behavior is configured by adapter tests."""

    def __init__(self) -> None:
        self.closed = False

    def Close(self) -> None:
        self.closed = True
