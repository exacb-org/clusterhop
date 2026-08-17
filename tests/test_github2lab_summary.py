# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Unit tests for GitHub Actions summary rendering."""

from __future__ import annotations

import pytest

from clusterhop.github2lab.logs import process_job_log
from clusterhop.github2lab.models import GitLabJob, GitLabPipeline
from clusterhop.github2lab.summary import (
    GitHubSummaryData,
    format_duration,
    format_status,
    render_github_summary,
)

PIPELINE_URL = "https://gitlab.example.test/group/project/-/pipelines/42"
JOB_URL = "https://gitlab.example.test/group/project/-/jobs/101"
DUMMY_PRIVATE_TOKEN = "dummy-private-token-that-must-not-appear"
DUMMY_TRIGGER_TOKEN = "dummy-trigger-token-that-must-not-appear"


def _pipeline(**overrides: object) -> GitLabPipeline:
    values: dict[str, object] = {
        "id": 42,
        "status": "success",
        "web_url": PIPELINE_URL,
        "ref": "ci/0123456789abcdef",
        "sha": "fedcba9876543210",
    }
    values.update(overrides)
    return GitLabPipeline(**values)  # type: ignore[arg-type]


def _job(**overrides: object) -> GitLabJob:
    values: dict[str, object] = {
        "id": 101,
        "name": "unit tests",
        "status": "success",
        "stage": "test",
        "duration": 3.5,
        "web_url": JOB_URL,
    }
    values.update(overrides)
    return GitLabJob(**values)  # type: ignore[arg-type]


def _summary_data(**overrides: object) -> GitHubSummaryData:
    main_job = _job()
    values: dict[str, object] = {
        "status": "success",
        "github_commit_sha": "0123456789abcdef",
        "github_repository": "example/source-project",
        "github_run_id": "987654",
        "gitlab_branch": "ci/0123456789abcdef",
        "pipeline": _pipeline(),
        "jobs": (main_job,),
        "selected_job": main_job,
        "processed_log": process_job_log(b"tests passed\n"),
    }
    values.update(overrides)
    return GitHubSummaryData(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("success", "✅ Success"),
        ("failed", "❌ Failed"),
        ("canceled", "🚫 Cancelled"),
        ("cancelled", "🚫 Cancelled"),
        ("skipped", "⏭️ Skipped"),
        ("timeout", "⏱️ Timed out"),
        ("timed_out", "⏱️ Timed out"),
        ("unknown", "❓ Unknown"),
        (None, "❓ Unknown"),
        ("", "❓ Unknown"),
    ],
)
def test_status_mapping(status: str | None, expected: str) -> None:
    assert format_status(status) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("success", "✅ Success"),
        ("failed", "❌ Failed"),
        ("canceled", "🚫 Cancelled"),
        ("cancelled", "🚫 Cancelled"),
        ("skipped", "⏭️ Skipped"),
        ("timeout", "⏱️ Timed out"),
        ("timed_out", "⏱️ Timed out"),
        ("unexpected", "❓ Unknown"),
    ],
)
def test_summary_renders_requested_status(status: str, expected: str) -> None:
    summary = render_github_summary(_summary_data(status=status))

    assert summary.startswith("## GitLab CI Result\n")
    assert f"**Status:** {expected}" in summary


def test_summary_contains_source_and_pipeline_details() -> None:
    summary = render_github_summary(_summary_data())

    assert "**GitHub commit:** `0123456789abcdef`" in summary
    assert "**GitHub repository:** example/source-project" in summary
    assert "**GitHub run ID:** `987654`" in summary
    assert "**GitLab branch:** `ci/0123456789abcdef`" in summary
    assert f"**GitLab pipeline:** [42]({PIPELINE_URL})" in summary
    assert "**GitLab pipeline SHA:** `fedcba9876543210`" in summary


def test_pipeline_without_url_is_rendered_without_link() -> None:
    summary = render_github_summary(_summary_data(pipeline=_pipeline(web_url=None)))

    assert "**GitLab pipeline:** 42" in summary
    assert "[42]" not in summary


def test_missing_pipeline_is_clear_and_uses_explicit_status() -> None:
    summary = render_github_summary(_summary_data(status="timeout", pipeline=None))

    assert "**Status:** ⏱️ Timed out" in summary
    assert "**GitLab pipeline:** Not available" in summary


def test_pipeline_status_is_used_when_explicit_status_is_missing() -> None:
    summary = render_github_summary(_summary_data(status=None, pipeline=_pipeline(status="failed")))

    assert "**Status:** ❌ Failed" in summary


def test_one_job_table_contains_link_and_formatted_duration() -> None:
    summary = render_github_summary(_summary_data())

    assert "**Total jobs:** 1" in summary
    assert "| Stage | Job | Status | Duration |" in summary
    assert f"| test | [unit tests]({JOB_URL}) | success | 3.50 s |" in summary


def test_multiple_jobs_are_all_rendered() -> None:
    first_job = _job()
    second_job = _job(
        id=102,
        name="package",
        stage="build",
        status="failed",
        duration=65.0,
        web_url=None,
    )

    summary = render_github_summary(
        _summary_data(jobs=(first_job, second_job), selected_job=second_job)
    )

    assert "**Total jobs:** 2" in summary
    assert f"| test | [unit tests]({JOB_URL}) | success | 3.50 s |" in summary
    assert "| build | package | failed | 1m 5s |" in summary


def test_no_jobs_has_clear_fallback() -> None:
    summary = render_github_summary(_summary_data(jobs=(), selected_job=None))

    assert "**Total jobs:** 0" in summary
    assert "No GitLab jobs were available for this pipeline." in summary
    assert "**Selected job:** Not available" in summary


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (0.0, "0.00 s"),
        (3.456, "3.46 s"),
        (65.0, "1m 5s"),
        (3661.0, "1h 1m 1s"),
        (None, "—"),
        (-1.0, "—"),
        (float("nan"), "—"),
    ],
)
def test_duration_formatting(duration: float | None, expected: str) -> None:
    assert format_duration(duration) == expected


def test_embeddable_log_is_included_with_size_information() -> None:
    processed_log = process_job_log(b"line one\nline two\n", summary_limit_bytes=100)

    summary = render_github_summary(_summary_data(processed_log=processed_log))

    assert "- **Cleaned log size:** 18 bytes (0.02 KiB)" in summary
    assert "- **Summary log limit:** 100 bytes (0.10 KiB)" in summary
    assert "```text\nline one\nline two\n\n```" in summary


def test_log_code_fence_cannot_be_closed_by_log_content() -> None:
    processed_log = process_job_log(b"before\n```\nafter")

    summary = render_github_summary(_summary_data(processed_log=processed_log))

    assert "````text\nbefore\n```\nafter\n````" in summary


def test_too_large_log_uses_fallback_and_job_link() -> None:
    full_log = b"sensitive full log content"
    processed_log = process_job_log(full_log, summary_limit_bytes=5)

    summary = render_github_summary(_summary_data(processed_log=processed_log))

    assert "too large for the GitHub summary" in summary
    assert full_log.decode("utf-8") not in summary
    assert f"[View the full job log in GitLab]({JOB_URL})" in summary


def test_unavailable_log_uses_fallback_without_size() -> None:
    processed_log = process_job_log(None)

    summary = render_github_summary(_summary_data(processed_log=processed_log))

    assert "- **Cleaned log size:** Unavailable" in summary
    assert "job log is unavailable" in summary
    assert f"[View the full job log in GitLab]({JOB_URL})" in summary


def test_markdown_table_cells_are_escaped() -> None:
    job = _job(
        stage="build|deploy\nnext",
        name="job|name[new]",
        status="failed|blocked",
        web_url=None,
    )

    summary = render_github_summary(_summary_data(jobs=(job,), selected_job=job))

    assert r"| build\|deploy next | job\|name\[new\] | failed\|blocked | 3.50 s |" in summary


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "javascript:alert(1)",
        "https://user:password@gitlab.example.test/job",
        "https://gitlab.example.test/job\nmalicious",
    ],
)
def test_unsafe_job_links_are_not_rendered(unsafe_url: str) -> None:
    job = _job(web_url=unsafe_url)

    summary = render_github_summary(_summary_data(jobs=(job,), selected_job=job))

    assert unsafe_url not in summary
    assert "[unit tests](" not in summary


def test_summary_has_no_secret_inputs_or_internal_details() -> None:
    summary = render_github_summary(_summary_data())

    assert DUMMY_PRIVATE_TOKEN not in summary
    assert DUMMY_TRIGGER_TOKEN not in summary
    assert "PRIVATE-TOKEN" not in summary
    assert "request headers" not in summary
    assert "GitHubSummaryData" not in summary
