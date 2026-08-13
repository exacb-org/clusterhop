# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""GitLab job-log processing for GitHub Actions output."""

from __future__ import annotations

import re
from dataclasses import dataclass

from clusterhop.github2lab.config import DEFAULT_LOG_SUMMARY_LIMIT_BYTES

BYTES_PER_KIB = 1024
LOG_UNAVAILABLE_MESSAGE = (
    "The GitLab job log is unavailable because it was not fetched or no job log exists."
)
LOG_UNAVAILABLE_AFTER_CANCELLATION_MESSAGE = (
    "The GitLab job log is unavailable because the workflow was cancelled before log "
    "collection completed."
)

# ECMA-48 control sequence introducers, operating-system commands (including terminal
# hyperlinks), single escape sequences, and their 8-bit CSI equivalent. Newlines and tabs
# remain intact because they are log content rather than terminal presentation instructions.
_ANSI_SEQUENCE_PATTERN = re.compile(
    "|".join(
        (
            r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)",
            r"\x1B\[[0-?]*[ -/]*[@-~]",
            r"\x9B[0-?]*[ -/]*[@-~]",
            r"\x1B[@-_]",
        )
    )
)
_ANSI_BYTE_SEQUENCE_PATTERN = re.compile(
    b"|".join(
        (
            rb"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)",
            rb"\x1B\[[0-?]*[ -/]*[@-~]",
            rb"\x9B[0-?]*[ -/]*[@-~]",
            rb"\x1B[@-_]",
        )
    )
)


@dataclass(frozen=True)
class ProcessedJobLog:
    """A summary-safe representation of a GitLab job trace."""

    text: str
    size_bytes: int | None
    size_kib: float | None
    summary_limit_bytes: int
    summary_limit_kib: float
    can_embed: bool
    is_unavailable: bool
    is_too_large: bool


def process_job_log(
    raw_trace: bytes | None,
    *,
    summary_limit_bytes: int = DEFAULT_LOG_SUMMARY_LIMIT_BYTES,
    unavailable_after_cancellation: bool = False,
) -> ProcessedJobLog:
    """Clean a raw GitLab trace and decide whether GitHub may embed it.

    ``None`` means that no trace was fetched. An empty byte string is a successfully fetched,
    empty trace and remains embeddable. Invalid UTF-8 is replaced safely so processing cannot
    fail because of malformed job output.
    """

    _validate_summary_limit(summary_limit_bytes)
    summary_limit_kib = bytes_to_kib(summary_limit_bytes)

    if raw_trace is None:
        message = (
            LOG_UNAVAILABLE_AFTER_CANCELLATION_MESSAGE
            if unavailable_after_cancellation
            else LOG_UNAVAILABLE_MESSAGE
        )
        return ProcessedJobLog(
            text=message,
            size_bytes=None,
            size_kib=None,
            summary_limit_bytes=summary_limit_bytes,
            summary_limit_kib=summary_limit_kib,
            can_embed=False,
            is_unavailable=True,
            is_too_large=False,
        )

    if not isinstance(raw_trace, bytes):
        raise TypeError("raw_trace must be bytes or None.")

    decoded_trace = _ANSI_BYTE_SEQUENCE_PATTERN.sub(b"", raw_trace).decode(
        "utf-8", errors="replace"
    )
    cleaned_trace = remove_ansi_sequences(decoded_trace)
    cleaned_size_bytes = len(cleaned_trace.encode("utf-8"))
    cleaned_size_kib = bytes_to_kib(cleaned_size_bytes)
    is_too_large = cleaned_size_bytes > summary_limit_bytes

    if is_too_large:
        text = (
            "The GitLab job log is too large for the GitHub summary "
            f"({cleaned_size_bytes} bytes / {cleaned_size_kib:.2f} KiB; "
            f"limit: {summary_limit_bytes} bytes / {summary_limit_kib:.2f} KiB). "
            "View the full log in GitLab."
        )
    else:
        text = cleaned_trace

    return ProcessedJobLog(
        text=text,
        size_bytes=cleaned_size_bytes,
        size_kib=cleaned_size_kib,
        summary_limit_bytes=summary_limit_bytes,
        summary_limit_kib=summary_limit_kib,
        can_embed=not is_too_large,
        is_unavailable=False,
        is_too_large=is_too_large,
    )


def remove_ansi_sequences(text: str) -> str:
    """Remove terminal presentation sequences while preserving ordinary log text."""

    if not isinstance(text, str):
        raise TypeError("text must be a string.")
    return _ANSI_SEQUENCE_PATTERN.sub("", text)


def bytes_to_kib(size_bytes: int) -> float:
    """Convert an exact byte count to kibibytes."""

    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise ValueError("size_bytes must be a non-negative integer.")
    return size_bytes / BYTES_PER_KIB


def _validate_summary_limit(summary_limit_bytes: object) -> None:
    if (
        isinstance(summary_limit_bytes, bool)
        or not isinstance(summary_limit_bytes, int)
        or summary_limit_bytes <= 0
    ):
        raise ValueError("summary_limit_bytes must be a positive integer.")
