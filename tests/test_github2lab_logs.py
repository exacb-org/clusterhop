# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Unit tests for GitLab job-log processing."""

from __future__ import annotations

import pytest

from clusterhop.github2lab.config import DEFAULT_LOG_SUMMARY_LIMIT_BYTES
from clusterhop.github2lab.logs import (
    LOG_UNAVAILABLE_AFTER_CANCELLATION_MESSAGE,
    LOG_UNAVAILABLE_MESSAGE,
    ProcessedJobLog,
    bytes_to_kib,
    process_job_log,
    remove_ansi_sequences,
)


def test_plain_log_is_returned_unchanged() -> None:
    raw_trace = b"build started\ntests passed\n"

    result = process_job_log(raw_trace)

    assert result.text == "build started\ntests passed\n"
    assert result.size_bytes == len(raw_trace)
    assert result.can_embed is True
    assert result.is_unavailable is False
    assert result.is_too_large is False


def test_ansi_color_and_control_sequences_are_removed() -> None:
    raw_trace = (
        b"\x1b[31mfailed\x1b[0m\n"
        b"\x1b]8;;https://gitlab.example.test/job\x07job link\x1b]8;;\x07\n"
        b"progress\x1b[2K\rcomplete\n"
        b"\x9b32msuccess\x9b0m\n"
    )

    result = process_job_log(raw_trace)

    assert result.text == "failed\njob link\nprogress\rcomplete\nsuccess\n"
    assert result.size_bytes == len(result.text.encode("utf-8"))


def test_invalid_utf8_bytes_are_decoded_safely() -> None:
    result = process_job_log(b"before\xffafter\n")

    assert result.text == "before\ufffdafter\n"
    assert result.size_bytes == len("before\ufffdafter\n".encode("utf-8"))
    assert result.can_embed is True


def test_small_log_is_fully_embeddable() -> None:
    result = process_job_log(b"small log", summary_limit_bytes=10)

    assert result == ProcessedJobLog(
        text="small log",
        size_bytes=9,
        size_kib=9 / 1024,
        summary_limit_bytes=10,
        summary_limit_kib=10 / 1024,
        can_embed=True,
        is_unavailable=False,
        is_too_large=False,
    )


def test_large_log_is_replaced_with_clear_message() -> None:
    full_log = b"x" * 11

    result = process_job_log(full_log, summary_limit_bytes=10)

    assert result.can_embed is False
    assert result.is_too_large is True
    assert result.is_unavailable is False
    assert result.size_bytes == 11
    assert result.size_kib == 11 / 1024
    assert "too large for the GitHub summary" in result.text
    assert "11 bytes" in result.text
    assert "limit: 10 bytes" in result.text
    assert result.text != full_log.decode("utf-8")


def test_log_at_exact_summary_limit_is_embeddable() -> None:
    full_log = b"x" * 10

    result = process_job_log(full_log, summary_limit_bytes=10)

    assert result.text == full_log.decode("utf-8")
    assert result.size_bytes == 10
    assert result.can_embed is True
    assert result.is_too_large is False


def test_unavailable_log_returns_standard_fallback() -> None:
    result = process_job_log(None, summary_limit_bytes=2048)

    assert result == ProcessedJobLog(
        text=LOG_UNAVAILABLE_MESSAGE,
        size_bytes=None,
        size_kib=None,
        summary_limit_bytes=2048,
        summary_limit_kib=2.0,
        can_embed=False,
        is_unavailable=True,
        is_too_large=False,
    )


def test_cancellation_related_missing_log_returns_specific_fallback() -> None:
    result = process_job_log(
        None,
        unavailable_after_cancellation=True,
    )

    assert result.text == LOG_UNAVAILABLE_AFTER_CANCELLATION_MESSAGE
    assert "cancelled before log collection completed" in result.text
    assert result.can_embed is False
    assert result.is_unavailable is True
    assert result.is_too_large is False


def test_empty_fetched_log_is_available_and_embeddable() -> None:
    result = process_job_log(b"")

    assert result.text == ""
    assert result.size_bytes == 0
    assert result.size_kib == 0.0
    assert result.can_embed is True
    assert result.is_unavailable is False


def test_default_summary_limit_comes_from_configuration_default() -> None:
    result = process_job_log(b"log")

    assert result.summary_limit_bytes == DEFAULT_LOG_SUMMARY_LIMIT_BYTES
    assert result.summary_limit_kib == DEFAULT_LOG_SUMMARY_LIMIT_BYTES / 1024


@pytest.mark.parametrize(
    ("size_bytes", "expected_kib"),
    [
        (0, 0.0),
        (1, 1 / 1024),
        (1024, 1.0),
        (1536, 1.5),
        (800_000, 781.25),
    ],
)
def test_bytes_to_kib_uses_binary_kibibytes(size_bytes: int, expected_kib: float) -> None:
    assert bytes_to_kib(size_bytes) == expected_kib


@pytest.mark.parametrize("size_bytes", [-1, 1.5, "1024", True])
def test_bytes_to_kib_rejects_invalid_sizes(size_bytes: object) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        bytes_to_kib(size_bytes)  # type: ignore[arg-type]


@pytest.mark.parametrize("summary_limit", [0, -1, 1.5, "10", True])
def test_process_job_log_rejects_invalid_summary_limit(summary_limit: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        process_job_log(b"log", summary_limit_bytes=summary_limit)  # type: ignore[arg-type]


def test_process_job_log_rejects_non_bytes_trace() -> None:
    with pytest.raises(TypeError, match="bytes or None"):
        process_job_log("not bytes")  # type: ignore[arg-type]


def test_remove_ansi_sequences_rejects_non_string() -> None:
    with pytest.raises(TypeError, match="must be a string"):
        remove_ansi_sequences(b"not text")  # type: ignore[arg-type]
