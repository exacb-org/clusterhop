# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Unit tests for cancellation state and signal handling."""

from __future__ import annotations

import signal
from collections.abc import Callable
from types import FrameType
from typing import Any, cast
from unittest.mock import patch

import pytest

from clusterhop.github2lab.cancellation import (
    CancellationCleanupError,
    CancellationController,
    CancellationError,
    CancellationSetupError,
)

SignalCallback = Callable[[int, FrameType | None], None]


class FakeSignalRegistry:
    """In-memory signal registry that never signals the test process."""

    def __init__(self) -> None:
        self.previous_sigint: object = object()
        self.previous_sigterm: object = object()
        self.handlers: dict[int, object] = {
            signal.SIGINT: self.previous_sigint,
            signal.SIGTERM: self.previous_sigterm,
        }
        self.set_calls: list[tuple[int, object]] = []

    def getsignal(self, signal_number: int) -> object:
        return self.handlers[signal_number]

    def set_signal(self, signal_number: int, handler: object) -> object:
        previous = self.handlers[signal_number]
        self.handlers[signal_number] = handler
        self.set_calls.append((signal_number, handler))
        return previous

    def send(self, signal_number: int) -> None:
        handler = cast(SignalCallback, self.handlers[signal_number])
        handler(signal_number, None)


def _signal_context(registry: FakeSignalRegistry) -> tuple[Any, Any]:
    return (
        patch("clusterhop.github2lab.cancellation.signal.getsignal", registry.getsignal),
        patch("clusterhop.github2lab.cancellation.signal.signal", registry.set_signal),
    )


@pytest.mark.parametrize("signal_number", [signal.SIGINT, signal.SIGTERM])
def test_supported_signal_requests_cancellation_and_runs_cleanup(signal_number: int) -> None:
    registry = FakeSignalRegistry()
    cleanup_calls: list[str] = []
    getsignal_patch, signal_patch = _signal_context(registry)

    with getsignal_patch, signal_patch:
        with CancellationController(lambda: cleanup_calls.append("cleanup")) as controller:
            registry.send(signal_number)

            assert controller.cancellation_requested is True
            assert controller.signal_number == signal_number
            assert controller.cleanup_started is True
            assert controller.cleanup_failed is False

    assert cleanup_calls == ["cleanup"]


def test_duplicate_signals_run_cleanup_only_once() -> None:
    registry = FakeSignalRegistry()
    cleanup_calls: list[str] = []
    getsignal_patch, signal_patch = _signal_context(registry)

    with getsignal_patch, signal_patch:
        with CancellationController(lambda: cleanup_calls.append("cleanup")) as controller:
            registry.send(signal.SIGINT)
            registry.send(signal.SIGTERM)
            registry.send(signal.SIGINT)

    assert cleanup_calls == ["cleanup"]
    assert controller.signal_number == signal.SIGINT


def test_no_pipeline_yet_cleanup_safely_does_nothing() -> None:
    runtime_state: dict[str, int | None] = {"pipeline_id": None}
    canceled_pipeline_ids: list[int] = []

    def cleanup() -> None:
        pipeline_id = runtime_state["pipeline_id"]
        if pipeline_id is not None:
            canceled_pipeline_ids.append(pipeline_id)

    controller = CancellationController(cleanup)
    controller.request_cancellation()

    assert controller.cancellation_requested is True
    assert controller.cleanup_started is True
    assert controller.cleanup_failed is False
    assert canceled_pipeline_ids == []


def test_cleanup_uses_current_state_supplied_by_runner() -> None:
    runtime_state: dict[str, int | None] = {"pipeline_id": None}
    canceled_pipeline_ids: list[int] = []

    def cleanup() -> None:
        pipeline_id = runtime_state["pipeline_id"]
        if pipeline_id is not None:
            canceled_pipeline_ids.append(pipeline_id)

    controller = CancellationController(cleanup)
    runtime_state["pipeline_id"] = 42

    controller.request_cancellation(signal.SIGTERM)

    assert canceled_pipeline_ids == [42]
    assert controller.signal_number == signal.SIGTERM


def test_cleanup_registered_after_cancellation_runs_immediately() -> None:
    cleanup_calls: list[str] = []
    controller = CancellationController()
    controller.request_cancellation(signal.SIGINT)

    assert controller.cleanup_started is False

    controller.register_cleanup(lambda: cleanup_calls.append("cleanup"))

    assert cleanup_calls == ["cleanup"]
    assert controller.cleanup_started is True


def test_cleanup_cannot_be_replaced_after_it_starts() -> None:
    controller = CancellationController(lambda: None)
    controller.request_cancellation()

    with pytest.raises(CancellationError, match="already started"):
        controller.register_cleanup(lambda: None)


def test_previous_signal_handlers_are_restored() -> None:
    registry = FakeSignalRegistry()
    getsignal_patch, signal_patch = _signal_context(registry)

    with getsignal_patch, signal_patch:
        with CancellationController():
            assert registry.handlers[signal.SIGINT] is not registry.previous_sigint
            assert registry.handlers[signal.SIGTERM] is not registry.previous_sigterm

        assert registry.handlers[signal.SIGINT] is registry.previous_sigint
        assert registry.handlers[signal.SIGTERM] is registry.previous_sigterm

    restored_signal_numbers = [call[0] for call in registry.set_calls[-2:]]
    assert restored_signal_numbers == [signal.SIGTERM, signal.SIGINT]


def test_handlers_are_restored_when_context_body_raises() -> None:
    registry = FakeSignalRegistry()
    getsignal_patch, signal_patch = _signal_context(registry)

    with getsignal_patch, signal_patch:
        with pytest.raises(RuntimeError, match="body failed"):
            with CancellationController():
                raise RuntimeError("body failed")

    assert registry.handlers[signal.SIGINT] is registry.previous_sigint
    assert registry.handlers[signal.SIGTERM] is registry.previous_sigterm


def test_cleanup_failure_is_stored_and_raised_without_secret_details() -> None:
    dummy_secret = "dummy-secret-that-must-not-appear"

    def failing_cleanup() -> None:
        raise RuntimeError(f"GitLab rejected {dummy_secret}")

    controller = CancellationController(failing_cleanup)

    controller.request_cancellation(signal.SIGTERM)

    assert controller.cancellation_requested is True
    assert controller.cleanup_started is True
    assert controller.cleanup_failed is True
    with pytest.raises(CancellationCleanupError) as captured:
        controller.raise_if_cleanup_failed()

    assert str(captured.value) == "Cancellation cleanup failed."
    assert dummy_secret not in str(captured.value)
    assert dummy_secret not in repr(captured.value)


def test_duplicate_signal_after_cleanup_failure_does_not_retry() -> None:
    cleanup_attempts: list[str] = []

    def failing_cleanup() -> None:
        cleanup_attempts.append("attempt")
        raise RuntimeError("dummy failure")

    controller = CancellationController(failing_cleanup)
    controller.request_cancellation(signal.SIGINT)
    controller.request_cancellation(signal.SIGTERM)

    assert cleanup_attempts == ["attempt"]


def test_controller_repr_does_not_expose_callback() -> None:
    dummy_secret = "dummy-secret-that-must-not-appear"

    def cleanup() -> None:
        del dummy_secret

    controller = CancellationController(cleanup)

    representation = repr(controller)

    assert representation == (
        "CancellationController(cancellation_requested=False, cleanup_started=False)"
    )
    assert dummy_secret not in representation


def test_register_cleanup_rejects_non_callable() -> None:
    controller = CancellationController()

    with pytest.raises(TypeError, match="must be callable"):
        controller.register_cleanup("not callable")  # type: ignore[arg-type]


def test_controller_cannot_enter_twice() -> None:
    registry = FakeSignalRegistry()
    controller = CancellationController()
    getsignal_patch, signal_patch = _signal_context(registry)

    with getsignal_patch, signal_patch:
        with controller:
            with pytest.raises(CancellationSetupError, match="already active"):
                controller.__enter__()
