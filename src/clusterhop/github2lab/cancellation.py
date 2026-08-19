# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Cancellation state and signal handling for active GitLab pipelines."""

from __future__ import annotations

import signal
from collections.abc import Callable
from types import FrameType, TracebackType
from typing import Any, Literal, cast

CleanupCallback = Callable[[], None]


class CancellationError(RuntimeError):
    """Base error for cancellation-controller failures."""


class CancellationSetupError(CancellationError):
    """Raised when signal handlers cannot be installed or restored safely."""


class CancellationCleanupError(CancellationError):
    """A sanitized error raised after cancellation cleanup fails."""


class CancellationController:
    """Track cancellation and coordinate one idempotent cleanup attempt.

    The controller deliberately knows nothing about GitLab. A runner supplies a callback whose
    closure can inspect the runner's current pipeline state and cancel an active pipeline when one
    exists. The callback is attempted at most once, even if both supported signals are received.
    """

    def __init__(self, cleanup: CleanupCallback | None = None) -> None:
        self._cleanup = cleanup
        self._cancellation_requested = False
        self._cleanup_started = False
        self._cleanup_error: CancellationCleanupError | None = None
        self._signal_number: int | None = None
        self._previous_handlers: dict[int, object] = {}
        self._is_active = False

    def __repr__(self) -> str:
        """Return state without exposing callback internals."""

        return (
            f"{type(self).__name__}("
            f"cancellation_requested={self._cancellation_requested}, "
            f"cleanup_started={self._cleanup_started})"
        )

    @property
    def cancellation_requested(self) -> bool:
        """Whether SIGINT, SIGTERM, or an explicit request has been received."""

        return self._cancellation_requested

    @property
    def cleanup_started(self) -> bool:
        """Whether a registered cleanup callback has been invoked."""

        return self._cleanup_started

    @property
    def cleanup_failed(self) -> bool:
        """Whether the cleanup callback raised an exception."""

        return self._cleanup_error is not None

    @property
    def signal_number(self) -> int | None:
        """The first signal number that requested cancellation, if any."""

        return self._signal_number

    def register_cleanup(self, cleanup: CleanupCallback) -> None:
        """Register the cleanup callback, running it if cancellation already arrived."""

        if not callable(cleanup):
            raise TypeError("cleanup must be callable.")
        if self._cleanup_started:
            raise CancellationError("Cancellation cleanup has already started.")
        self._cleanup = cleanup
        if self._cancellation_requested:
            self._attempt_cleanup()

    def request_cancellation(self, signal_number: int | None = None) -> None:
        """Latch cancellation and attempt the registered cleanup exactly once."""

        if self._cancellation_requested:
            return
        self._cancellation_requested = True
        self._signal_number = signal_number
        self._attempt_cleanup()

    def raise_if_cleanup_failed(self) -> None:
        """Raise the sanitized cleanup error, if cleanup failed."""

        if self._cleanup_error is not None:
            raise self._cleanup_error from None

    def __enter__(self) -> CancellationController:
        """Install temporary SIGINT and SIGTERM handlers."""

        if self._is_active:
            raise CancellationSetupError("Cancellation controller is already active.")

        installed_signals: list[int] = []
        try:
            for signal_number in (signal.SIGINT, signal.SIGTERM):
                self._previous_handlers[signal_number] = signal.getsignal(signal_number)
                signal.signal(signal_number, self._handle_signal)
                installed_signals.append(signal_number)
        except (OSError, RuntimeError, ValueError):
            self._restore_handlers(installed_signals, suppress_errors=True)
            self._previous_handlers.clear()
            raise CancellationSetupError(
                "Unable to install cancellation signal handlers."
            ) from None

        self._is_active = True
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Restore every signal handler saved on context entry."""

        del exception_type, exception, traceback
        previous_handlers = tuple(self._previous_handlers)
        try:
            self._restore_handlers(previous_handlers, suppress_errors=False)
        finally:
            self._previous_handlers.clear()
            self._is_active = False
        return False

    def _handle_signal(self, signal_number: int, frame: FrameType | None) -> None:
        del frame
        self.request_cancellation(signal_number)

    def _attempt_cleanup(self) -> None:
        cleanup = self._cleanup
        if cleanup is None or self._cleanup_started:
            return

        # Set this before invoking user code so a second signal cannot re-enter cleanup.
        self._cleanup_started = True
        try:
            cleanup()
        except Exception:
            self._cleanup_error = CancellationCleanupError("Cancellation cleanup failed.")

    def _restore_handlers(
        self,
        signal_numbers: tuple[int, ...] | list[int],
        *,
        suppress_errors: bool,
    ) -> None:
        restoration_failed = False
        for signal_number in reversed(signal_numbers):
            previous_handler = self._previous_handlers.get(signal_number)
            if previous_handler is None:
                continue
            try:
                signal.signal(signal_number, cast(Any, previous_handler))
            except (OSError, RuntimeError, ValueError):
                restoration_failed = True

        if restoration_failed and not suppress_errors:
            raise CancellationSetupError("Unable to restore cancellation signal handlers.")
