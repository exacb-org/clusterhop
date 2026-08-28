# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Orchestration for the complete GitHub-to-GitLab bridge flow."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Literal, Protocol

from clusterhop.github2lab.cancellation import (
    CancellationCleanupError,
    CancellationController,
)
from clusterhop.github2lab.config import GitHub2LabConfig, load_config_from_environment
from clusterhop.github2lab.git import GitLabBranchPusher
from clusterhop.github2lab.gitlab import GitLabClient
from clusterhop.github2lab.logs import ProcessedJobLog, process_job_log
from clusterhop.github2lab.models import GitLabJob, GitLabPipeline
from clusterhop.github2lab.state import (
    FilePipelineStateStore,
    NullPipelineStateStore,
    PipelineState,
    PipelineStateStorage,
    is_terminal_pipeline_status,
    pipeline_state_from_config,
)
from clusterhop.github2lab.summary import GitHubSummaryData, render_github_summary

RunnerStatus = Literal["success", "failed", "canceled", "skipped", "timeout", "error"]
SummaryRenderer = Callable[[GitHubSummaryData], str]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]

_TERMINAL_STATUS_MAP: Mapping[str, RunnerStatus] = {
    "success": "success",
    "failed": "failed",
    "canceled": "canceled",
    "cancelled": "canceled",
    "skipped": "skipped",
    "timeout": "timeout",
    "timed_out": "timeout",
}
_FALLBACK_SUMMARY = (
    "## GitLab CI Result\n\n"
    "**Status:** ❓ Unknown\n\n"
    "The GitHub Actions summary could not be generated.\n"
)


class RunnerError(RuntimeError):
    """A sanitized orchestration failure."""


class SummaryWriteError(RunnerError):
    """A sanitized failure to write the GitHub Actions summary."""


class GitPusher(Protocol):
    """Git operation required by the runner."""

    def push_current_commit(self) -> str:
        """Push the configured commit and return its GitLab branch."""


class GitLabOperations(Protocol):
    """GitLab operations required by the runner."""

    def trigger_pipeline(self, ref: str) -> GitLabPipeline:
        """Trigger a pipeline for the supplied ref."""

    def get_pipeline(self, pipeline_id: int) -> GitLabPipeline:
        """Get the latest pipeline details."""

    def get_pipeline_jobs(self, pipeline_id: int) -> tuple[GitLabJob, ...]:
        """Get every job belonging to a pipeline."""

    def get_job_trace(self, job_id: int) -> bytes:
        """Get the raw trace for a job."""

    def cancel_pipeline(
        self,
        pipeline_id: int,
        *,
        timeout_seconds: float | None = None,
    ) -> GitLabPipeline:
        """Cancel an active pipeline."""


class CancellationCoordinator(Protocol):
    """Cancellation mechanism consumed by the runner."""

    @property
    def cancellation_requested(self) -> bool:
        """Whether cancellation has been requested."""

    def register_cleanup(self, cleanup: Callable[[], None]) -> None:
        """Register the runner's active-pipeline cleanup callback."""

    def raise_if_cleanup_failed(self) -> None:
        """Raise a sanitized cleanup error when cleanup failed."""

    def __enter__(self) -> CancellationCoordinator:
        """Activate cancellation monitoring."""

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Deactivate cancellation monitoring."""


class SummaryWriter(Protocol):
    """Destination for rendered GitHub Actions Markdown."""

    def write(self, summary: str) -> None:
        """Write one complete summary."""


class NullSummaryWriter:
    """Summary destination used when no GitHub summary file is available."""

    def write(self, summary: str) -> None:
        del summary


class FileSummaryWriter:
    """Append Markdown to the file supplied by GitHub Actions."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def write(self, summary: str) -> None:
        """Append a UTF-8 summary without exposing paths in failures."""

        try:
            with self._path.open("a", encoding="utf-8", newline="\n") as summary_file:
                summary_file.write(summary)
        except (OSError, UnicodeError):
            raise SummaryWriteError("Unable to write the GitHub Actions summary.") from None


@dataclass(frozen=True)
class GitHub2LabResult:
    """Final, secret-safe result of one github2lab run."""

    status: RunnerStatus
    branch_name: str
    pipeline: GitLabPipeline | None
    jobs: tuple[GitLabJob, ...]
    selected_job: GitLabJob | None
    processed_log: ProcessedJobLog = field(repr=False)
    summary: str = field(repr=False)
    error_message: str | None = field(default=None, repr=False)

    @property
    def exit_code(self) -> int:
        """Return zero only when the GitLab pipeline succeeded."""

        return 0 if self.status == "success" else 1


@dataclass
class _RuntimeState:
    gitlab: GitLabOperations
    config: GitHub2LabConfig
    state_store: PipelineStateStorage
    pipeline: GitLabPipeline | None = None
    persisted_state: PipelineState | None = None
    pipeline_is_terminal: bool = False
    cancellation_attempted: bool = False
    cancellation_cleanup_failed: bool = False

    def update_pipeline(self, pipeline: GitLabPipeline) -> None:
        if self.pipeline is not None and pipeline.id != self.pipeline.id:
            raise RunnerError("GitLab returned an unexpected pipeline identifier.")
        if self.pipeline_is_terminal and _normalize_terminal_status(pipeline.status) is None:
            # A request that was in flight when cancellation completed may return stale,
            # non-terminal data. Never regress a terminal pipeline back to an active state.
            return
        self.pipeline = pipeline
        self.pipeline_is_terminal = _normalize_terminal_status(pipeline.status) is not None
        persistent_state = self.persisted_state
        if persistent_state is None:
            persistent_state = pipeline_state_from_config(
                self.config,
                pipeline,
                active=not self.pipeline_is_terminal,
            )
        else:
            persistent_state = persistent_state.with_pipeline(
                pipeline,
                active=not self.pipeline_is_terminal,
            )
        self.state_store.write(persistent_state)
        self.persisted_state = persistent_state

    def cancel_active_pipeline(self) -> None:
        pipeline = self.pipeline
        if pipeline is None or self.pipeline_is_terminal or self.cancellation_attempted:
            return
        self.cancellation_attempted = True
        try:
            canceled_pipeline = self.gitlab.cancel_pipeline(
                pipeline.id,
                timeout_seconds=self.config.cleanup_timeout_seconds,
            )
        except Exception:
            self.cancellation_cleanup_failed = True
            raise
        self.update_pipeline(canceled_pipeline)


class _CancellationRequested(Exception):
    def __init__(self, *, cleanup_failed: bool = False) -> None:
        super().__init__("Cancellation requested.")
        self.cleanup_failed = cleanup_failed


class GitHub2LabRunner:
    """Coordinate the complete GitHub-to-GitLab bridge flow."""

    def __init__(
        self,
        config: GitHub2LabConfig,
        *,
        git_pusher: GitPusher | None = None,
        gitlab: GitLabOperations | None = None,
        cancellation: CancellationCoordinator | None = None,
        summary_writer: SummaryWriter | None = None,
        summary_renderer: SummaryRenderer = render_github_summary,
        state_store: PipelineStateStorage | None = None,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        self._config = config
        self._git_pusher = git_pusher if git_pusher is not None else GitLabBranchPusher(config)
        self._gitlab = gitlab if gitlab is not None else GitLabClient(config)
        self._cancellation = cancellation if cancellation is not None else CancellationController()
        self._summary_writer = summary_writer if summary_writer is not None else NullSummaryWriter()
        self._summary_renderer = summary_renderer
        self._state_store = (
            state_store if state_store is not None else _state_store_from_config(config)
        )
        self._clock = clock
        self._sleeper = sleeper
        self._has_run = False

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def run(self) -> GitHub2LabResult:
        """Run the bridge once and always produce a structured final result."""

        if self._has_run:
            raise RunnerError("A GitHub2LabRunner instance can only run once.")
        self._has_run = True

        expected_branch = f"ci/{self._config.github_commit_sha}"
        branch_name = expected_branch
        state = _RuntimeState(self._gitlab, self._config, self._state_store)
        jobs: tuple[GitLabJob, ...] = ()
        selected_job: GitLabJob | None = None
        raw_trace: bytes | None = None
        status: RunnerStatus = "error"
        error_message: str | None = None
        operation = "starting the GitHub-to-GitLab bridge"
        timed_out = False

        self._cancellation.register_cleanup(state.cancel_active_pipeline)
        with self._cancellation:
            try:
                self._checkpoint_cancellation(state)

                operation = "pushing the GitHub commit to GitLab"
                branch_name = self._git_pusher.push_current_commit()
                if branch_name != expected_branch:
                    raise RunnerError("Git push returned an unexpected GitLab branch.")
                self._checkpoint_cancellation(state)

                operation = "triggering the GitLab pipeline"
                pipeline = self._gitlab.trigger_pipeline(branch_name)
                state.update_pipeline(pipeline)
                self._checkpoint_cancellation(state)

                operation = "waiting for the GitLab pipeline"
                status = self._wait_for_pipeline(state)
                timed_out = status == "timeout"
                if timed_out:
                    operation = "cancelling the timed-out GitLab pipeline"
                    try:
                        state.cancel_active_pipeline()
                    except Exception:
                        error_message = "GitLab pipeline timed out, but cleanup failed."
                else:
                    self._checkpoint_cancellation(state)

                completed_pipeline = state.pipeline
                if completed_pipeline is None:
                    raise RunnerError("GitLab pipeline state is unavailable.")

                operation = "fetching GitLab pipeline jobs"
                jobs = self._gitlab.get_pipeline_jobs(completed_pipeline.id)
                self._checkpoint_cancellation(state)

                selected_job = select_main_job(jobs)
                if selected_job is not None:
                    operation = "fetching the selected GitLab job log"
                    try:
                        raw_trace = self._gitlab.get_job_trace(selected_job.id)
                    except Exception:
                        raw_trace = None
                    self._checkpoint_cancellation(state)
            except _CancellationRequested as cancellation:
                if timed_out:
                    status = "timeout"
                    if cancellation.cleanup_failed:
                        error_message = "GitLab pipeline timed out, but cleanup failed."
                else:
                    status = "canceled"
                    error_message = (
                        "Cancellation cleanup failed."
                        if cancellation.cleanup_failed
                        else "Cancellation was requested."
                    )
            except Exception:
                status = "error"
                error_message = f"github2lab failed while {operation}."

            status, error_message = self._apply_pending_cancellation(state, status, error_message)
            processed_log = process_job_log(
                raw_trace,
                summary_limit_bytes=self._config.log_summary_limit_bytes,
                unavailable_after_cancellation=self._cancellation.cancellation_requested,
            )
            summary_data = self._summary_data(
                status=status,
                branch_name=branch_name,
                state=state,
                jobs=jobs,
                selected_job=selected_job,
                processed_log=processed_log,
            )

            try:
                summary = self._summary_renderer(summary_data)
            except Exception:
                status = "error"
                error_message = "Unable to generate the GitHub Actions summary."
                summary = _FALLBACK_SUMMARY

            status, error_message = self._apply_pending_cancellation(state, status, error_message)
            if status != summary_data.status:
                summary_data = self._summary_data(
                    status=status,
                    branch_name=branch_name,
                    state=state,
                    jobs=jobs,
                    selected_job=selected_job,
                    processed_log=processed_log,
                )
                try:
                    summary = self._summary_renderer(summary_data)
                except Exception:
                    status = "error"
                    error_message = "Unable to generate the GitHub Actions summary."
                    summary = _FALLBACK_SUMMARY

            try:
                self._summary_writer.write(summary)
            except Exception:
                status = "error"
                error_message = "Unable to write the GitHub Actions summary."

            status, error_message = self._apply_pending_cancellation(state, status, error_message)
            if status != summary_data.status:
                if raw_trace is None and self._cancellation.cancellation_requested:
                    processed_log = process_job_log(
                        None,
                        summary_limit_bytes=self._config.log_summary_limit_bytes,
                        unavailable_after_cancellation=True,
                    )
                summary_data = self._summary_data(
                    status=status,
                    branch_name=branch_name,
                    state=state,
                    jobs=jobs,
                    selected_job=selected_job,
                    processed_log=processed_log,
                )
                try:
                    summary = self._summary_renderer(summary_data)
                except Exception:
                    status = "error"
                    error_message = "Unable to generate the GitHub Actions summary."
                    summary = _FALLBACK_SUMMARY

        return GitHub2LabResult(
            status=status,
            branch_name=branch_name,
            pipeline=state.pipeline,
            jobs=jobs,
            selected_job=selected_job,
            processed_log=processed_log,
            summary=summary,
            error_message=error_message,
        )

    def _wait_for_pipeline(self, state: _RuntimeState) -> RunnerStatus:
        pipeline = state.pipeline
        if pipeline is None:
            raise RunnerError("GitLab pipeline state is unavailable.")

        deadline = self._clock() + self._config.pipeline_timeout_seconds
        while True:
            self._checkpoint_cancellation(state)
            pipeline = self._gitlab.get_pipeline(pipeline.id)
            state.update_pipeline(pipeline)
            terminal_status = _normalize_terminal_status(pipeline.status)
            if terminal_status is not None:
                return terminal_status

            remaining_seconds = deadline - self._clock()
            if remaining_seconds <= 0:
                return "timeout"
            self._checkpoint_cancellation(state)
            self._sleeper(min(self._config.poll_interval_seconds, remaining_seconds))
            self._checkpoint_cancellation(state)

    def _checkpoint_cancellation(self, state: _RuntimeState) -> None:
        if not self._cancellation.cancellation_requested:
            return

        cleanup_failed = state.cancellation_cleanup_failed
        try:
            # A signal may have arrived while trigger_pipeline was running, before its response
            # supplied an ID. Re-checking here safely cancels once that ID is known.
            state.cancel_active_pipeline()
        except Exception:
            cleanup_failed = True
        try:
            self._cancellation.raise_if_cleanup_failed()
        except CancellationCleanupError:
            cleanup_failed = True
        raise _CancellationRequested(cleanup_failed=cleanup_failed)

    def _apply_pending_cancellation(
        self,
        state: _RuntimeState,
        status: RunnerStatus,
        error_message: str | None,
    ) -> tuple[RunnerStatus, str | None]:
        try:
            self._checkpoint_cancellation(state)
        except _CancellationRequested as cancellation:
            if status == "timeout":
                return (
                    "timeout",
                    (
                        "GitLab pipeline timed out, but cleanup failed."
                        if cancellation.cleanup_failed
                        else error_message
                    ),
                )
            return (
                "canceled",
                (
                    "Cancellation cleanup failed."
                    if cancellation.cleanup_failed
                    else "Cancellation was requested."
                ),
            )
        return status, error_message

    def _summary_data(
        self,
        *,
        status: RunnerStatus,
        branch_name: str,
        state: _RuntimeState,
        jobs: tuple[GitLabJob, ...],
        selected_job: GitLabJob | None,
        processed_log: ProcessedJobLog,
    ) -> GitHubSummaryData:
        return GitHubSummaryData(
            status=status,
            github_commit_sha=self._config.github_commit_sha,
            github_repository=self._config.github_repository,
            github_run_id=self._config.github_run_id,
            gitlab_branch=branch_name,
            pipeline=state.pipeline,
            jobs=jobs,
            selected_job=selected_job,
            processed_log=processed_log,
        )


def select_main_job(jobs: tuple[GitLabJob, ...]) -> GitLabJob | None:
    """Select the first GitLab job, preserving the prototype's simple behavior."""

    return jobs[0] if jobs else None


def run_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    repository_path: str | Path | None = None,
    summary_writer: SummaryWriter | None = None,
) -> GitHub2LabResult:
    """Load validated environment configuration and run the production bridge."""

    source = os.environ if environ is None else environ
    config = load_config_from_environment(source)
    writer = (
        summary_writer if summary_writer is not None else _summary_writer_from_environment(source)
    )
    return GitHub2LabRunner(
        config,
        git_pusher=GitLabBranchPusher(config, repository_path=repository_path),
        gitlab=GitLabClient(config),
        summary_writer=writer,
    ).run()


def _summary_writer_from_environment(environ: Mapping[str, str]) -> SummaryWriter:
    summary_path = environ.get("GITHUB_STEP_SUMMARY")
    if summary_path is None or not summary_path.strip():
        return NullSummaryWriter()
    return FileSummaryWriter(summary_path)


def _state_store_from_config(config: GitHub2LabConfig) -> PipelineStateStorage:
    if config.pipeline_state_file is None:
        return NullPipelineStateStore()
    return FilePipelineStateStore(config.pipeline_state_file)


def _normalize_terminal_status(status: str) -> RunnerStatus | None:
    normalized_status = status.strip().casefold()
    if not is_terminal_pipeline_status(normalized_status):
        return None
    return _TERMINAL_STATUS_MAP.get(normalized_status, "error")
