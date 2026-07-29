"""Fault-injection tests for exact owner generation failure handling."""

from __future__ import annotations

from pathlib import Path
import threading

import pytest

from touptek4040.camera_contract import ReadoutMode, Roi
from touptek4040.camera_owner import OwnerStartupSpec
from touptek4040.camera_proxy import CameraOwnerProxy, OwnerProcessFactory
from touptek4040.config import OwnerConfig
from touptek4040.errors import (
    CameraError,
    CameraInvalidValueError,
    CameraNotConnectedError,
    CameraOwnerProtocolError,
    CameraOwnerTerminationError,
    CameraTimeoutError,
)

from test_camera_proxy import (
    FakeProcess,
    FakeRegistry,
    ScriptedFactory,
    make_proxy,
)


@pytest.mark.parametrize("operation", ["connect", "get_gain", "disconnect"])
def test_timed_out_command_is_sent_once_and_terminates_only_exact_child(
    tmp_path: Path, operation: str
):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    if operation != "connect":
        proxy.connect()
    generation = proxy.owner_generation
    failed_process = factory.sessions[-1].process
    factory.behaviors[(generation, operation)] = "hang"

    with pytest.raises(CameraTimeoutError):
        if operation == "connect":
            proxy.connect()
        elif operation == "get_gain":
            _ = proxy.gain
        else:
            proxy.disconnect()

    assert factory.calls[operation] == 1
    assert failed_process.terminate_calls == 1
    assert failed_process.kill_calls == 0
    assert proxy.last_terminated_pid == failed_process.pid
    assert proxy.owner_generation == generation + 1
    assert proxy.connected is False


def test_timed_out_exposure_is_not_retried_or_published(tmp_path: Path):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    generation = proxy.owner_generation
    factory.behaviors[(generation, "acquire")] = "hang"

    with pytest.raises(CameraTimeoutError):
        proxy.acquire(0.001, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12)

    assert factory.calls["acquire"] == 1
    assert factory.calls["release_frame"] == 0
    assert proxy.connected is False
    assert proxy.confirmed_heater_level is None


@pytest.mark.parametrize(
    "corruption",
    [
        "stale_ack",
        "wrong_version_ack",
        "wrong_request_ack",
        "wrong_operation_ack",
        "malformed_ack",
        "nonfinite_ack",
        "overflow_ack",
        "out_of_range_ack",
        "duplicate_ack",
    ],
)
def test_invalid_acquire_start_ack_fails_exact_generation_without_retry(
    tmp_path: Path, corruption: str
):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    generation = proxy.owner_generation
    failed_process = factory.sessions[-1].process
    factory.behaviors[(generation, "acquire")] = corruption

    with pytest.raises(CameraOwnerProtocolError):
        proxy.acquire(
            0.001, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12
        )

    assert factory.calls["acquire"] == 1
    assert factory.calls["release_frame"] == 0
    assert failed_process.terminate_calls == 1
    assert proxy.owner_generation == generation + 1
    assert proxy.connected is False


@pytest.mark.parametrize("behavior", ["missing_ack", "late_ack"])
def test_missing_or_late_acquire_start_ack_times_out_without_retry(
    tmp_path: Path, behavior: str
):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    generation = proxy.owner_generation
    failed_process = factory.sessions[-1].process
    factory.behaviors[(generation, "acquire")] = behavior

    with pytest.raises(CameraTimeoutError):
        proxy.acquire(
            0.001, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12
        )

    assert factory.calls["acquire"] == 1
    assert failed_process.terminate_calls == 1
    assert proxy.owner_generation == generation + 1
    assert proxy.connected is False


def test_timed_out_heater_write_is_not_retried_and_invalidates_confirmation(tmp_path: Path):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    assert proxy.heater_level == 2
    generation = proxy.owner_generation
    factory.behaviors[(generation, "set_heater_level")] = "hang"

    with pytest.raises(CameraTimeoutError):
        proxy.heater_level = 3

    assert factory.calls["set_heater_level"] == 1
    assert proxy.confirmed_heater_level is None
    assert proxy.connected is False
    with pytest.raises(CameraNotConnectedError):
        proxy.heater_level = 4
    assert factory.calls["set_heater_level"] == 1


@pytest.mark.parametrize("corruption", ["stale_generation", "wrong_request", "duplicate", "exit"])
def test_protocol_corruption_or_owner_exit_replaces_generation(
    tmp_path: Path, corruption: str
):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    generation = proxy.owner_generation
    factory.behaviors[(generation, "get_gain")] = corruption
    if corruption == "duplicate":
        assert proxy.gain == 100
        operation = "get_temperature"
        factory.behaviors[(generation, operation)] = "ok"
    else:
        operation = "get_gain"

    with pytest.raises((CameraOwnerProtocolError, CameraTimeoutError)):
        if operation == "get_gain":
            _ = proxy.gain
        else:
            _ = proxy.temperature_c

    assert proxy.owner_generation == generation + 1
    assert proxy.connected is False
    assert factory.sessions[-2].process.terminate_calls == 1


def test_invalid_frame_descriptor_fails_generation_without_release_or_frame(tmp_path: Path):
    factory = ScriptedFactory()
    original_result = factory.result_for

    def bad_result(operation, payload):
        result = original_result(operation, payload)
        if operation == "acquire":
            result["descriptor"]["nbytes"] = 7
        return result

    factory.result_for = bad_result
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    generation = proxy.owner_generation

    with pytest.raises(CameraOwnerProtocolError):
        proxy.acquire(0.001, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12)

    assert proxy.owner_generation == generation + 1
    assert factory.calls["acquire"] == 1
    assert factory.calls["release_frame"] == 0


def test_invalid_ordinary_result_schema_is_a_generation_failure(tmp_path: Path):
    factory = ScriptedFactory()
    original_result = factory.result_for

    def bad_result(operation, payload):
        if operation == "get_gain":
            return {"value": "not-an-integer"}
        return original_result(operation, payload)

    factory.result_for = bad_result
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    generation = proxy.owner_generation

    with pytest.raises(CameraOwnerProtocolError):
        _ = proxy.gain

    assert proxy.owner_generation == generation + 1
    assert proxy.connected is False


def test_orphaned_or_unreadable_segment_cleans_failed_generation(tmp_path: Path):
    registries: list[BrokenRegistry] = []

    class BrokenRegistry(FakeRegistry):
        def __init__(self, generation, path):
            super().__init__(generation, path)
            registries.append(self)

        def consume(self, descriptor):
            self.consumed.append(descriptor)
            raise OSError("mapping vanished after descriptor publication")

    factory = ScriptedFactory()
    proxy = make_proxy(
        tmp_path, factory, registry_factory=BrokenRegistry
    )
    proxy.connect()
    generation = proxy.owner_generation

    with pytest.raises(CameraOwnerProtocolError):
        proxy.acquire(0.001, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12)

    failed_registry = next(
        registry for registry in registries if registry.generation == generation
    )
    assert len(failed_registry.consumed) == 1
    assert failed_registry.cleaned == [generation]
    assert factory.calls["release_frame"] == 0
    assert proxy.owner_generation == generation + 1


def test_release_ack_timeout_discards_copied_frame_and_cleans_generation(tmp_path: Path):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    generation = proxy.owner_generation
    factory.behaviors[(generation, "release_frame")] = "hang"

    with pytest.raises(CameraTimeoutError):
        proxy.acquire(0.001, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12)

    assert factory.calls["acquire"] == 1
    assert factory.calls["release_frame"] == 1
    assert proxy.owner_generation == generation + 1
    assert proxy.connected is False


def test_abort_timeout_uses_control_deadline_and_replaces_owner(tmp_path: Path):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    generation = proxy.owner_generation
    factory.behaviors[(generation, "abort")] = "hang"

    with pytest.raises(CameraTimeoutError):
        proxy.abort()

    assert factory.calls["abort"] == 1
    assert proxy.owner_generation == generation + 1
    assert proxy.connected is False


def test_exact_child_is_killed_only_when_it_resists_terminate(tmp_path: Path):
    class StubbornProcess(FakeProcess):
        def terminate(self) -> None:
            self.terminate_calls += 1

    class StubbornFactory(ScriptedFactory):
        def start(self, startup_spec, generation, registry_path):
            session = super().start(
                startup_spec, generation, registry_path
            )
            if generation == 1:
                stubborn = StubbornProcess(session.process.pid)
                session.process = stubborn
            return session

    factory = StubbornFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    generation = proxy.owner_generation
    failed_process = factory.sessions[-1].process
    factory.behaviors[(generation, "get_gain")] = "hang"

    with pytest.raises(CameraTimeoutError):
        _ = proxy.gain

    assert failed_process.terminate_calls == 1
    assert failed_process.kill_calls == 1
    assert failed_process.exitcode == -9


def test_concurrent_abort_and_acquire_failure_replace_one_generation_only(
    tmp_path: Path,
):
    factory = ScriptedFactory()
    proxy = make_proxy(
        tmp_path,
        factory,
        owner_config=OwnerConfig(
                startup_timeout_s=0.05,
                connect_timeout_s=0.05,
                command_timeout_s=0.5,
                exposure_overhead_s=0.2,
                exposure_multiplier=1.0,
                abort_timeout_s=0.05,
                disconnect_timeout_s=0.05,
        ),
    )
    proxy.connect()
    generation = proxy.owner_generation
    factory.behaviors[(generation, "acquire")] = "hang"
    factory.behaviors[(generation, "abort")] = "hang"
    acquire_errors: list[BaseException] = []

    def acquire() -> None:
        try:
            proxy.acquire(
                0.001, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12
            )
        except BaseException as error:
            acquire_errors.append(error)

    thread = threading.Thread(target=acquire)
    thread.start()
    while factory.calls["acquire"] == 0:
        thread.join(0.001)

    with pytest.raises(CameraTimeoutError):
        proxy.abort()
    thread.join(1.0)

    assert thread.is_alive() is False
    assert len(acquire_errors) == 1
    assert isinstance(
        acquire_errors[0], (CameraTimeoutError, CameraOwnerProtocolError)
    )
    assert proxy.owner_generation == generation + 1
    assert len(factory.sessions) == 2


def test_failed_generation_cannot_resurrect_confirmed_heater_level(
    tmp_path: Path,
):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    generation = proxy.owner_generation
    factory.behaviors[(generation, "abort")] = "hang"
    validated = threading.Event()
    release_validation = threading.Event()
    original_validate = proxy._supervisor.validate

    def paused_validate(session, validator):
        result = original_validate(session, validator)
        validated.set()
        assert release_validation.wait(1.0)
        return result

    proxy._supervisor.validate = paused_validate
    results: list[object] = []

    def read_heater() -> None:
        try:
            results.append(proxy.heater_level)
        except BaseException as error:
            results.append(error)

    thread = threading.Thread(target=read_heater)
    thread.start()
    assert validated.wait(1.0)
    with pytest.raises(CameraTimeoutError):
        proxy.abort()
    release_validation.set()
    thread.join(1.0)

    assert thread.is_alive() is False
    assert len(results) == 1
    assert isinstance(results[0], CameraNotConnectedError)
    assert proxy.owner_generation == generation + 1
    assert proxy.confirmed_heater_level is None


def test_registry_cleanup_error_does_not_block_state_invalidation_or_recovery(
    tmp_path: Path,
):
    class CleanupFailingRegistry(FakeRegistry):
        failed = False

        def cleanup_generation(self, generation):
            if not type(self).failed:
                type(self).failed = True
                raise OSError("injected registry cleanup failure")
            super().cleanup_generation(generation)

    factory = ScriptedFactory()
    proxy = make_proxy(
        tmp_path, factory, registry_factory=CleanupFailingRegistry
    )
    proxy.connect()
    assert proxy.heater_level == 2
    generation = proxy.owner_generation
    factory.behaviors[(generation, "get_gain")] = "hang"

    with pytest.raises(CameraTimeoutError):
        _ = proxy.gain

    assert proxy.owner_generation == generation + 1
    assert proxy.connected is False
    assert proxy.confirmed_heater_level is None
    assert proxy.last_cleanup_error is not None


def test_startup_timeout_terminates_exact_child_and_recovers_with_fresh_generation(
    tmp_path: Path,
):
    factory = ScriptedFactory()
    factory.behaviors[(1, "handshake")] = "hang"

    proxy = make_proxy(tmp_path, factory)

    assert len(factory.sessions) == 2
    assert factory.sessions[0].process.terminate_calls == 1
    assert proxy.owner_generation == 2
    assert proxy.connected is False


def test_handshake_requires_exact_integer_generation_before_publication(
    tmp_path: Path,
):
    factory = ScriptedFactory()
    original_result = factory.result_for

    def malformed_first_handshake(operation, payload):
        result = original_result(operation, payload)
        if operation == "handshake" and len(factory.sessions) == 1:
            result["generation"] = True
        return result

    factory.result_for = malformed_first_handshake
    proxy = make_proxy(tmp_path, factory)

    assert len(factory.sessions) == 2
    assert factory.sessions[0].process.terminate_calls == 1
    assert proxy.owner_generation == 2


def test_two_failures_crossing_old_identity_check_create_one_replacement(
    tmp_path: Path,
):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    supervisor = proxy._supervisor
    old_session = supervisor._session
    terminate_entered = threading.Event()
    allow_terminate = threading.Event()
    original_terminate = old_session.process.terminate

    def gated_terminate():
        terminate_entered.set()
        assert allow_terminate.wait(1.0)
        return original_terminate()

    old_session.process.terminate = gated_terminate
    errors: list[BaseException] = []

    def fail(label: str) -> None:
        try:
            supervisor._fail_owner(
                CameraTimeoutError(label), failed_session=old_session
            )
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=fail, args=("first",))
    second = threading.Thread(target=fail, args=("second",))
    first.start()
    assert terminate_entered.wait(1.0)
    second.start()
    allow_terminate.set()
    first.join(1.0)
    second.join(1.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(errors) == 2
    assert len(factory.sessions) == 2
    assert old_session.process.terminate_calls == 1
    assert proxy.owner_generation == 2


def test_stale_semantic_validation_cannot_fail_replacement(tmp_path: Path):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    supervisor = proxy._supervisor
    old_session = supervisor._session
    with pytest.raises(CameraTimeoutError):
        supervisor._fail_owner(
            CameraTimeoutError("old failed"),
            failed_session=old_session,
        )
    replacement = supervisor._session

    with pytest.raises(CameraOwnerProtocolError):
        supervisor.validate(
            old_session,
            lambda: (_ for _ in ()).throw(
                CameraOwnerProtocolError("stale malformed response")
            ),
        )

    assert supervisor._session is replacement
    assert replacement.process.terminate_calls == 0


def test_failed_runtime_replacement_handshake_is_not_retried_or_published(
    tmp_path: Path,
):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    factory.behaviors[(1, "get_gain")] = "hang"
    factory.behaviors[(2, "handshake")] = "hang"

    with pytest.raises(CameraTimeoutError):
        _ = proxy.gain

    assert len(factory.sessions) == 2
    assert proxy._supervisor._session is None
    assert factory.sessions[1].process.terminate_calls == 1


@pytest.mark.parametrize("operation", ["abort", "request", "acquire"])
def test_authorization_and_session_capture_are_atomic_across_rotation(
    tmp_path: Path, operation: str
):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    supervisor = proxy._supervisor
    old_session = supervisor._session
    entered = threading.Event()
    resume = threading.Event()
    original_capture = supervisor._capture_authorized_session

    def paused_capture(*args, **kwargs):
        entered.set()
        assert resume.wait(1.0)
        return original_capture(*args, **kwargs)

    supervisor._capture_authorized_session = paused_capture
    results: list[BaseException | object] = []

    def invoke() -> None:
        try:
            if operation == "abort":
                results.append(proxy.abort())
            elif operation == "request":
                results.append(proxy.gain)
            else:
                results.append(
                    proxy.acquire(
                        0.001,
                        Roi(0, 0, 2, 2),
                        1,
                        ReadoutMode.RAW12,
                    )
                )
        except BaseException as error:
            results.append(error)

    thread = threading.Thread(target=invoke)
    thread.start()
    assert entered.wait(1.0)
    with pytest.raises(CameraTimeoutError):
        supervisor._fail_owner(
            CameraTimeoutError("rotate at former capture gap"),
            failed_session=old_session,
        )
    replacement = supervisor._session
    resume.set()
    thread.join(1.0)

    assert not thread.is_alive()
    assert len(results) == 1
    assert isinstance(results[0], CameraNotConnectedError)
    assert factory.calls[operation if operation != "request" else "get_gain"] == 0
    assert len(factory.sessions) == 2
    assert proxy.owner_generation == 2
    assert replacement.process.terminate_calls == 0


@pytest.mark.parametrize(
    "requested_mode,malformed",
    [
        (ReadoutMode.RAW12, False),
        (ReadoutMode.RAW12, 0.0),
        (ReadoutMode.RAW8, True),
        (ReadoutMode.RAW8, 1.0),
    ],
)
def test_frame_readout_mode_requires_exact_integer_representation(
    tmp_path: Path, requested_mode: ReadoutMode, malformed: object
):
    registries: list[FakeRegistry] = []

    class TrackingRegistry(FakeRegistry):
        def __init__(self, generation, path):
            super().__init__(generation, path)
            registries.append(self)

    factory = ScriptedFactory()
    original_result = factory.result_for

    def malformed_result(operation, payload):
        result = original_result(operation, payload)
        if operation == "acquire":
            result["frame"]["readout_mode"] = malformed
            if requested_mode is ReadoutMode.RAW8:
                result["descriptor"].update(
                    {
                        "dtype": "uint8",
                        "nbytes": 4,
                        "max_adu": 255,
                        "metadata": {
                            "pixel_format": "RAW8",
                            "max_adu": 255,
                        },
                    }
                )
        return result

    factory.result_for = malformed_result
    proxy = make_proxy(
        tmp_path, factory, registry_factory=TrackingRegistry
    )
    proxy.connect()
    generation = proxy.owner_generation

    with pytest.raises(CameraOwnerProtocolError):
        proxy.acquire(
            0.001, Roi(0, 0, 2, 2), 1, requested_mode
        )

    failed_registry = next(
        registry for registry in registries
        if registry.generation == generation
    )
    assert failed_registry.consumed == []
    assert failed_registry.cleaned == [generation]
    assert factory.calls["release_frame"] == 0
    assert proxy.owner_generation == generation + 1


def test_constructor_cleans_partial_candidates_when_endpoint_property_raises(
    tmp_path: Path,
):
    class RaisingEndpointSession:
        def __init__(self, session):
            self.process = session.process
            self._command = session.command_connection
            self._control = session.control_connection
            self._raised = False

        @property
        def command_connection(self):
            if not self._raised:
                self._raised = True
                raise OSError("injected command endpoint property failure")
            return self._command

        @property
        def control_connection(self):
            return self._control

    class RaisingEndpointFactory(ScriptedFactory):
        def start(self, startup_spec, generation, registry_path):
            session = super().start(
                startup_spec, generation, registry_path
            )
            return RaisingEndpointSession(session)

    factory = RaisingEndpointFactory()

    with pytest.raises(CameraError):
        make_proxy(tmp_path, factory)

    assert len(factory.sessions) == 2
    for session in factory.sessions:
        assert session.process.terminate_calls == 1
        assert session.command_connection.closed
        assert session.control_connection.closed


def test_runtime_replacement_pid_failure_is_cleaned_and_never_published(
    tmp_path: Path,
):
    class RaisingPidProcess(FakeProcess):
        def __init__(self, pid: int) -> None:
            self._safe_pid = pid
            self._pid_reads = 0
            super().__init__(pid)

        @property
        def pid(self):
            self._pid_reads += 1
            if self._pid_reads >= 2:
                raise OSError("injected pid validation failure")
            return self._safe_pid

        @pid.setter
        def pid(self, value):
            self._safe_pid = value

    class RaisingPidFactory(ScriptedFactory):
        def start(self, startup_spec, generation, registry_path):
            session = super().start(
                startup_spec, generation, registry_path
            )
            if generation == 2:
                session.process = RaisingPidProcess(
                    session.process.pid
                )
            return session

    factory = RaisingPidFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    factory.behaviors[(1, "get_gain")] = "hang"

    with pytest.raises(CameraError):
        _ = proxy.gain

    assert len(factory.sessions) == 2
    assert proxy._supervisor._session is None
    candidate = factory.sessions[1]
    assert candidate.process.terminate_calls == 1
    assert candidate.command_connection.closed
    assert candidate.control_connection.closed


def test_close_retries_partial_unsafe_candidate_and_is_idempotent(
    tmp_path: Path,
):
    class DelayedProcessSession:
        def __init__(self, session):
            self._process = session.process
            self.command_connection = session.command_connection
            self.control_connection = session.control_connection
            self._reads = 0

        @property
        def process(self):
            self._reads += 1
            if self._reads <= 2:
                raise OSError("injected transient process property failure")
            return self._process

    class DelayedProcessFactory(ScriptedFactory):
        def start(self, startup_spec, generation, registry_path):
            session = super().start(
                startup_spec, generation, registry_path
            )
            if generation == 2:
                return DelayedProcessSession(session)
            return session

    factory = DelayedProcessFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    factory.behaviors[(1, "get_gain")] = "hang"

    with pytest.raises(CameraOwnerTerminationError):
        _ = proxy.gain

    proxy.close()
    proxy.close()

    candidate = factory.sessions[1]
    assert candidate.process.terminate_calls == 1
    assert candidate.command_connection.closed
    assert candidate.control_connection.closed
    assert proxy._supervisor._unsafe_session is None


def test_unexpected_shutdown_exception_cannot_bypass_close_termination(
    tmp_path: Path,
):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    session = proxy._supervisor._session
    original_send = session.command_connection.send

    def raising_shutdown(payload):
        if payload.get("operation") == "shutdown":
            raise RuntimeError("injected unexpected shutdown failure")
        return original_send(payload)

    session.command_connection.send = raising_shutdown

    proxy.close()
    proxy.close()

    assert session.process.terminate_calls == 1
    assert not session.process.is_alive()
    assert session.command_connection.closed
    assert session.control_connection.closed
    assert proxy._supervisor._session is None
    assert proxy._supervisor._unsafe_session is None


@pytest.mark.parametrize("raising_child", ["command", "control"])
def test_process_factory_post_start_endpoint_failure_cannot_orphan_child(
    tmp_path: Path, raising_child: str
):
    class Endpoint:
        def __init__(self, *, raises: bool = False):
            self.raises = raises
            self.close_calls = 0
            self.closed = False

        def close(self):
            self.close_calls += 1
            if self.raises and self.close_calls == 1:
                raise OSError("injected child endpoint close failure")
            self.closed = True

    class StartedProcess(FakeProcess):
        def __init__(self):
            super().__init__(9100)
            self.start_calls = 0

        def start(self):
            self.start_calls += 1

    command_parent = Endpoint()
    command_child = Endpoint(raises=raising_child == "command")
    control_parent = Endpoint()
    control_child = Endpoint(raises=raising_child == "control")
    process = StartedProcess()

    class Context:
        def __init__(self):
            self.pipes = [
                (command_parent, command_child),
                (control_parent, control_child),
            ]

        def Pipe(self, duplex=True):
            assert duplex is True
            return self.pipes.pop(0)

        def Process(self, **kwargs):
            del kwargs
            return process

    factory = OwnerProcessFactory(Context())

    with pytest.raises(Exception):
        factory.start(OwnerStartupSpec(), 1, tmp_path / "frames.json")

    assert process.start_calls == 1
    assert process.terminate_calls >= 1
    assert not process.is_alive()
    assert all(
        endpoint.closed
        for endpoint in (
            command_parent,
            command_child,
            control_parent,
            control_child,
        )
    )


@pytest.mark.parametrize("failure", ["resists_kill", "kill_throws", "join_throws"])
def test_unstoppable_exact_child_blocks_replacement_with_stable_error(
    tmp_path: Path, failure: str
):
    class UnsafeProcess(FakeProcess):
        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1
            if failure == "kill_throws":
                raise OSError("injected kill failure")

        def join(self, timeout=None) -> None:
            if failure == "join_throws":
                raise OSError("injected join failure")
            super().join(timeout)

    class UnsafeFactory(ScriptedFactory):
        def start(self, startup_spec, generation, registry_path):
            session = super().start(
                startup_spec, generation, registry_path
            )
            if generation == 1:
                session.process = UnsafeProcess(session.process.pid)
            return session

    factory = UnsafeFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    factory.behaviors[(1, "get_gain")] = "hang"

    with pytest.raises(CameraOwnerTerminationError):
        _ = proxy.gain

    assert len(factory.sessions) == 1
    assert proxy.connected is False
    assert proxy.last_termination_error is not None
    assert factory.sessions[0].process.is_alive()
    with pytest.raises(CameraOwnerTerminationError):
        proxy.close()


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_descriptor",
        "missing_frame",
        "descriptor_shape_mismatch",
        "roi_mismatch",
        "binning_mismatch",
        "zero_frame_binning",
        "mode_mismatch",
        "non_utc_timestamp",
        "negative_frame_origin",
        "zero_frame_roi",
        "dtype_mode_mismatch",
        "overflow_descriptor_metadata",
        "overflow_frame_duration",
    ],
)
def test_malformed_final_frame_semantics_fail_captured_generation(
    tmp_path: Path, corruption: str
):
    factory = ScriptedFactory()
    original_result = factory.result_for

    def malformed_result(operation, payload):
        result = original_result(operation, payload)
        if operation != "acquire":
            return result
        if corruption == "missing_descriptor":
            result.pop("descriptor")
        elif corruption == "missing_frame":
            result.pop("frame")
        elif corruption == "descriptor_shape_mismatch":
            result["descriptor"]["shape"] = [1, 4]
        elif corruption == "roi_mismatch":
            result["frame"]["roi"]["width"] = 1
        elif corruption == "binning_mismatch":
            result["frame"]["binning"] = 2
        elif corruption == "zero_frame_binning":
            result["frame"]["binning"] = 0
        elif corruption == "mode_mismatch":
            result["frame"]["readout_mode"] = 1
        elif corruption == "non_utc_timestamp":
            result["frame"]["started_utc"] = "2026-07-29T12:00:00+08:00"
        elif corruption == "negative_frame_origin":
            result["frame"]["roi"]["x"] = -1
        elif corruption == "zero_frame_roi":
            result["frame"]["roi"]["width"] = 0
        elif corruption == "dtype_mode_mismatch":
            result["descriptor"]["dtype"] = "uint8"
            result["descriptor"]["nbytes"] = 4
            result["descriptor"]["max_adu"] = 255
            result["descriptor"]["metadata"] = {
                "pixel_format": "RAW8",
                "max_adu": 255,
            }
        elif corruption == "overflow_descriptor_metadata":
            result["descriptor"]["metadata"]["max_adu"] = 10**1000
        elif corruption == "overflow_frame_duration":
            result["frame"]["duration_s"] = 10**1000
        return result

    factory.result_for = malformed_result
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    generation = proxy.owner_generation

    with pytest.raises(CameraOwnerProtocolError):
        proxy.acquire(
            0.001, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12
        )

    assert factory.calls["acquire"] == 1
    assert proxy.owner_generation == generation + 1
    assert proxy.connected is False


def test_negative_release_ack_is_fatal_and_never_returns_frame(tmp_path: Path):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    generation = proxy.owner_generation
    factory.behaviors[(generation, "release_frame")] = "error"

    with pytest.raises(CameraOwnerProtocolError):
        proxy.acquire(
            0.001, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12
        )

    assert factory.calls["acquire"] == 1
    assert factory.calls["release_frame"] == 1
    assert proxy.owner_generation == generation + 1


def test_caller_numeric_overflow_is_rejected_before_ipc(tmp_path: Path):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()

    with pytest.raises(CameraInvalidValueError):
        proxy.acquire(
            10**1000, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12
        )

    assert factory.calls["acquire"] == 0


def test_caller_setting_numeric_overflow_is_rejected_before_ipc(
    tmp_path: Path,
):
    factory = ScriptedFactory()
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()

    with pytest.raises(CameraInvalidValueError):
        proxy.target_temperature_c = 10**1000

    assert factory.calls["set_target_temperature"] == 0


def test_ordinary_response_numeric_overflow_fails_generation(tmp_path: Path):
    factory = ScriptedFactory()
    original_result = factory.result_for

    def overflow_result(operation, payload):
        if operation == "get_temperature":
            return {"value": 10**1000}
        return original_result(operation, payload)

    factory.result_for = overflow_result
    proxy = make_proxy(tmp_path, factory)
    proxy.connect()
    generation = proxy.owner_generation

    with pytest.raises(CameraOwnerProtocolError):
        _ = proxy.temperature_c

    assert proxy.owner_generation == generation + 1


def test_capability_numeric_overflow_fails_generation(tmp_path: Path):
    factory = ScriptedFactory()
    original_result = factory.result_for

    def overflow_result(operation, payload):
        result = original_result(operation, payload)
        if operation == "capabilities":
            result["pixel_size_x"] = 10**1000
        return result

    factory.result_for = overflow_result
    proxy = make_proxy(tmp_path, factory)
    generation = proxy.owner_generation

    with pytest.raises(CameraOwnerProtocolError):
        _ = proxy.capabilities

    assert proxy.owner_generation == generation + 1


def test_acknowledged_duration_deadline_overflow_fails_generation(
    tmp_path: Path,
):
    factory = ScriptedFactory()
    factory.actual_duration_s = 3600.0
    proxy = make_proxy(
        tmp_path,
        factory,
        owner_config=OwnerConfig(exposure_multiplier=1e308),
    )
    proxy.connect()
    generation = proxy.owner_generation

    with pytest.raises(CameraOwnerProtocolError):
        proxy.acquire(
            0.001, Roi(0, 0, 2, 2), 1, ReadoutMode.RAW12
        )

    assert factory.calls["acquire"] == 1
    assert proxy.owner_generation == generation + 1
