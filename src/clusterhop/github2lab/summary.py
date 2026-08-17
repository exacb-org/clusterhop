# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""GitHub Actions summary generation for GitLab pipeline results."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from clusterhop.github2lab.logs import ProcessedJobLog
from clusterhop.github2lab.models import GitLabJob, GitLabPipeline

_STATUS_LABELS = {
    "success": "✅ Success",
    "failed": "❌ Failed",
    "canceled": "🚫 Cancelled",
    "cancelled": "🚫 Cancelled",
    "skipped": "⏭️ Skipped",
    "timeout": "⏱️ Timed out",
    "timed_out": "⏱️ Timed out",
}
_BACKTICK_RUN_PATTERN = re.compile(r"`+")


@dataclass(frozen=True)
class GitHubSummaryData:
    """User-facing data needed to render one GitHub Actions summary."""

    status: str | None
    github_commit_sha: str
    github_repository: str
    github_run_id: str
    gitlab_branch: str
    pipeline: GitLabPipeline | None
    jobs: tuple[GitLabJob, ...]
    selected_job: GitLabJob | None
    processed_log: ProcessedJobLog


def render_github_summary(data: GitHubSummaryData) -> str:
    """Render a compact GitHub Actions Markdown summary."""

    pipeline = data.pipeline
    effective_status = (
        data.status
        if data.status is not None
        else (pipeline.status if pipeline is not None else None)
    )
    lines = [
        "## GitLab CI Result",
        "",
        f"**Status:** {format_status(effective_status)}",
        "",
        "### Run details",
        "",
        f"- **GitHub commit:** `{_escape_inline_code(data.github_commit_sha)}`",
        f"- **GitHub repository:** {_escape_markdown_text(data.github_repository)}",
        f"- **GitHub run ID:** `{_escape_inline_code(data.github_run_id)}`",
        f"- **GitLab branch:** `{_escape_inline_code(data.gitlab_branch)}`",
    ]

    if pipeline is None:
        lines.append("- **GitLab pipeline:** Not available")
    else:
        pipeline_label = str(pipeline.id)
        pipeline_link = _markdown_link(pipeline_label, pipeline.web_url)
        lines.append(f"- **GitLab pipeline:** {pipeline_link}")
        if pipeline.sha:
            lines.append(f"- **GitLab pipeline SHA:** `{_escape_inline_code(pipeline.sha)}`")

    lines.extend(_render_jobs(data.jobs))
    lines.extend(_render_log(data.selected_job, data.processed_log))
    return "\n".join(lines).rstrip() + "\n"


def format_status(status: str | None) -> str:
    """Map a GitLab or bridge result to its user-facing status label."""

    if not isinstance(status, str) or not status.strip():
        return "❓ Unknown"
    return _STATUS_LABELS.get(status.strip().casefold(), "❓ Unknown")


def format_duration(duration_seconds: float | None) -> str:
    """Format a GitLab job duration for a compact jobs table."""

    if (
        duration_seconds is None
        or isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not math.isfinite(duration_seconds)
        or duration_seconds < 0
    ):
        return "—"

    duration = float(duration_seconds)
    if duration < 60:
        return f"{duration:.2f} s"

    whole_seconds = int(round(duration))
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"


def _render_jobs(jobs: Sequence[GitLabJob]) -> list[str]:
    lines = ["", "### Jobs", "", f"**Total jobs:** {len(jobs)}", ""]
    if not jobs:
        lines.append("No GitLab jobs were available for this pipeline.")
        return lines

    lines.extend(
        [
            "| Stage | Job | Status | Duration |",
            "|---|---|---|---:|",
        ]
    )
    for job in jobs:
        stage = _escape_table_cell(job.stage)
        name = _markdown_link(_escape_table_cell(job.name), job.web_url)
        status = _escape_table_cell(job.status)
        duration = format_duration(job.duration)
        lines.append(f"| {stage} | {name} | {status} | {duration} |")
    return lines


def _render_log(selected_job: GitLabJob | None, processed_log: ProcessedJobLog) -> list[str]:
    lines = ["", "### Main job log", ""]
    if selected_job is None:
        lines.append("**Selected job:** Not available")
    else:
        selected_job_label = _escape_markdown_text(selected_job.name)
        lines.append(
            f"**Selected job:** {_markdown_link(selected_job_label, selected_job.web_url)}"
        )

    size_description = (
        "Unavailable"
        if processed_log.size_bytes is None or processed_log.size_kib is None
        else f"{processed_log.size_bytes} bytes ({processed_log.size_kib:.2f} KiB)"
    )
    lines.extend(
        [
            "",
            f"- **Cleaned log size:** {size_description}",
            "- **Summary log limit:** "
            f"{processed_log.summary_limit_bytes} bytes "
            f"({processed_log.summary_limit_kib:.2f} KiB)",
            "",
        ]
    )

    if processed_log.can_embed:
        fence = _code_fence_for(processed_log.text)
        lines.extend((f"{fence}text", processed_log.text, fence))
    else:
        lines.append(_escape_markdown_text(processed_log.text))
        if selected_job is not None and _is_safe_web_url(selected_job.web_url):
            lines.extend(
                ("", _markdown_link("View the full job log in GitLab", selected_job.web_url))
            )
    return lines


def _markdown_link(label: str, url: str | None) -> str:
    if not _is_safe_web_url(url):
        return label
    assert url is not None
    safe_url = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return f"[{label}]({safe_url})"


def _is_safe_web_url(url: str | None) -> bool:
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and "\n" not in url
        and "\r" not in url
    )


def _escape_table_cell(value: str) -> str:
    return _escape_markdown_text(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def _escape_markdown_text(value: str) -> str:
    escaped = value.replace("\\", r"\\")
    for character in ("`", "*", "_", "[", "]", "<", ">", "#", "!"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _escape_inline_code(value: str) -> str:
    return value.replace("`", "ˋ").replace("\r", " ").replace("\n", " ")


def _code_fence_for(text: str) -> str:
    longest_run = max(
        (len(match.group()) for match in _BACKTICK_RUN_PATTERN.finditer(text)), default=0
    )
    return "`" * max(3, longest_run + 1)
