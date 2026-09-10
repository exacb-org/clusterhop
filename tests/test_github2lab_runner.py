# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Unit tests for the github2lab orchestration layer."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Literal
from unittest.mock import patch

import pytest

from clusterhop.github2lab.cancellation import CancellationCleanupError
from clusterhop.github2lab.config import GitHub2LabConfig
from clusterhop.github2lab.models import GitLabJob, GitLabPipeline
from clusterhop.github2lab.runner import (
    FileSummaryWriter,
    GitHub2LabRunner,
    RunnerError,
    SummaryWriteError,
    select_log_job,
)
from clusterhop.github2lab.summary import GitHubSummaryData, render_github_summary
from clusterhop.github2lab.state import PipelineState, PipelineStateStorage

GITHUB_COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"
BRANCH_NAME = f"ci/{GITHUB_COMMIT_SHA}"
DUMMY_GITLAB_TOKEN = "dummy-private-token"
DUMMY_TRIGGER_TOKEN = "dummy-trigger-token"


def _config(**overrides: object) -> GitHub2LabConfig:
    values: dict[str, object] = {
        "gitlab_token": DUMMY_GITLAB_TOKEN,
        "gitlab_trigger_token": DUMMY_TRIGGER_TOKEN,
        "gitlab_url": "https://gitlab.example.test",
        "gitlab_remote_url": "git@gitlab.example.test:group/project.git",
        "gitlab_project_id": "1234",
        "gitlab_project_path": None,
        "github_commit_sha": GITHUB_COMMIT_SHA,
        "github_repository": "example/source-project",
        "github_run_id": "987654",
        "poll_interval_seconds": 2.0,
        "pipeline_timeout_seconds": 5.0,
        "log_summary_limit_bytes": 800_000,
    }
    values.update(overrides)
    return GitHub2LabConfig(**values)  # type: ignore[arg-type]


def _pipeline(status: str = "running", **overrides: object) -> GitLabPipeline:
    values: dict[str, object] = {
        "id": 42,
        "status": status,
        "web_url": "https://gitlab.example.test/group/project/-/pipelines/42",
        "ref": BRANCH_NAME,
        "sha": GITHUB_COMMIT_SHA,
    }
    values.update(overrides)
    return GitLabPipeline(**values)  # type: ignore[arg-type]


def _job(job_id: int = 101, name: str = "test", **overrides: object) -> GitLabJob:
    values: dict[str, object] = {
        "id": job_id,
        "name": name,
        "status": "success",
        "stage": "test",
        "duration": 3.5,
        "web_url": f"https://gitlab.example.test/group/project/-/jobs/{job_id}",
    }
    values.update(overrides)
    return GitLabJob(**values)  # type: ignore[arg-type]


class FakeCancellationController:
    """Cancellation coordinator with no operating-system signal interaction."""

    def __init__(self) -> None:
        self.cancellation_requested = False
        self.cleanup_attempted = False
        self.entered = False
        self.exited = False
        self._cleanup: Callable[[], None] | None = None
        self._cleanup_error: CancellationCleanupError | None = None

    def register_cleanup(self, cleanup: Callable[[], None]) -> None:
        self._cleanup = cleanup
        if self.cancellation_requested:
            self._attempt_cleanup()

    def request_cancellation(self) -> None:
        if self.cancellation_requested:
            return
        self.cancellation_requested = True
        self._attempt_cleanup()

    def raise_if_cleanup_failed(self) -> None:
        if self._cleanup_error is not None:
            raise self._cleanup_error from None

    def __enter__(self) -> FakeCancellationController:
        self.entered = True
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exception_type, exception, traceback
        self.exited = True
        return False

    def _attempt_cleanup(self) -> None:
        if self._cleanup is None or self.cleanup_attempted:
            return
        self.cleanup_attempted = True
        try:
            self._cleanup()
        except Exception:
            self._cleanup_error = CancellationCleanupError("Cancellation cleanup failed.")


class FakeGitPusher:
    def __init__(
        self,
        *,
        branch_name: str = BRANCH_NAME,
        hook: Callable[[], None] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.branch_name = branch_name
        self.hook = hook
        self.error = error
        self.calls = 0

    def push_current_commit(self) -> str:
        self.calls += 1
        if self.hook is not None:
            self.hook()
        if self.error is not None:
            raise self.error
        return self.branch_name


class FakeGitLabClient:
    def __init__(
        self,
        *,
        trigger_pipeline: GitLabPipeline | None = None,
        pipeline_updates: list[GitLabPipeline] | None = None,
        jobs: tuple[GitLabJob, ...] = (),
        trace: bytes = b"",
        cancel_result: GitLabPipeline | None = None,
    ) -> None:
        self.trigger_result = trigger_pipeline or _pipeline("pending")
        self.pipeline_updates = list(pipeline_updates or [_pipeline("success")])
        self.jobs = jobs
        self.trace = trace
        self.cancel_result = cancel_result or _pipeline("canceled")
        self.hooks: dict[str, Callable[[], None]] = {}
        self.errors: dict[str, Exception] = {}
        self.calls: list[tuple[str, object]] = []
        self.cancel_pipeline_ids: list[int] = []
        self.cancel_timeouts: list[float | None] = []
        self._last_pipeline = self.pipeline_updates[-1]

    def trigger_pipeline(self, ref: str) -> GitLabPipeline:
        self.calls.append(("trigger", ref))
        self._before_return("trigger")
        return self.trigger_result

    def get_pipeline(self, pipeline_id: int) -> GitLabPipeline:
        self.calls.append(("get_pipeline", pipeline_id))
        self._before_return("get_pipeline")
        if self.pipeline_updates:
            self._last_pipeline = self.pipeline_updates.pop(0)
        return self._last_pipeline

    def get_pipeline_jobs(self, pipeline_id: int) -> tuple[GitLabJob, ...]:
        self.calls.append(("get_jobs", pipeline_id))
        self._before_return("get_jobs")
        return self.jobs

    def get_job_trace(self, job_id: int) -> bytes:
        self.calls.append(("get_trace", job_id))
        self._before_return("get_trace")
        return self.trace

    def cancel_pipeline(
        self,
        pipeline_id: int,
        *,
        timeout_seconds: float | None = None,
    ) -> GitLabPipeline:
        self.calls.append(("cancel", pipeline_id))
        self.cancel_pipeline_ids.append(pipeline_id)
        self.cancel_timeouts.append(timeout_seconds)
        self._before_return("cancel")
        return self.cancel_result

    def _before_return(self, operation: str) -> None:
        hook = self.hooks.get(operation)
        if hook is not None:
            hook()
        error = self.errors.get(operation)
        if error is not None:
            raise error


class FakeClock:
    def __init__(self) -> None:
        self.current = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


class FakeSummaryWriter:
    def __init__(
        self,
        *,
        hook: Callable[[], None] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.hook = hook
        self.error = error
        self.summaries: list[str] = []

    def write(self, summary: str) -> None:
        self.summaries.append(summary)
        if self.hook is not None:
            self.hook()
        if self.error is not None:
            raise self.error


class RecordingStateStore:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.states: list[PipelineState] = []

    def read(self) -> PipelineState | None:
        return self.states[-1] if self.states else None

    def write(self, state: PipelineState) -> None:
        if self.error is not None:
            raise self.error
        self.states.append(state)


def _runner(
    *,
    config: GitHub2LabConfig | None = None,
    pusher: FakeGitPusher | None = None,
    gitlab: FakeGitLabClient | None = None,
    cancellation: FakeCancellationController | None = None,
    writer: FakeSummaryWriter | None = None,
    renderer: Callable[[GitHubSummaryData], str] = render_github_summary,
    clock: FakeClock | None = None,
    state_store: PipelineStateStorage | None = None,
) -> GitHub2LabRunner:
    fake_clock = clock or FakeClock()
    return GitHub2LabRunner(
        config or _config(),
        git_pusher=pusher or FakeGitPusher(),
        gitlab=gitlab or FakeGitLabClient(),
        cancellation=cancellation or FakeCancellationController(),
        summary_writer=writer or FakeSummaryWriter(),
        summary_renderer=renderer,
        state_store=state_store,
        clock=fake_clock.monotonic,
        sleeper=fake_clock.sleep,
    )


def test_successful_run_connects_all_workers() -> None:
    first_job = _job()
    second_job = _job(102, "package", stage="build", duration=65.0)
    gitlab = FakeGitLabClient(
        pipeline_updates=[_pipeline("running"), _pipeline("success")],
        jobs=(first_job, second_job),
        trace=b"\x1b[32mtests passed\x1b[0m\n",
    )
    clock = FakeClock()
    writer = FakeSummaryWriter()
    cancellation = FakeCancellationController()

    result = _runner(
        gitlab=gitlab,
        clock=clock,
        writer=writer,
        cancellation=cancellation,
    ).run()

    assert result.status == "success"
    assert result.exit_code == 0
    assert result.branch_name == BRANCH_NAME
    assert result.pipeline == _pipeline("success")
    assert result.jobs == (first_job, second_job)
    assert result.selected_job is None
    assert result.processed_log.is_unavailable is True
    assert "**Status:** ✅ Success" in result.summary
    assert "**Total jobs:** 2" in result.summary
    assert "### Job log" not in result.summary
    assert writer.summaries == [result.summary]
    assert clock.sleeps == [2.0]
    assert gitlab.calls == [
        ("trigger", BRANCH_NAME),
        ("get_pipeline", 42),
        ("get_pipeline", 42),
        ("get_jobs", 42),
    ]
    assert cancellation.entered is True
    assert cancellation.exited is True


def test_single_job_run_fetches_and_embeds_its_log() -> None:
    job = _job()
    gitlab = FakeGitLabClient(
        pipeline_updates=[_pipeline("success")],
        jobs=(job,),
        trace=b"tests passed\n",
    )

    result = _runner(gitlab=gitlab).run()

    assert result.selected_job == job
    assert result.processed_log.text == "tests passed\n"
    assert ("get_trace", job.id) in gitlab.calls
    assert "### Job log" in result.summary
    assert "tests passed" in result.summary


@pytest.mark.parametrize(
    ("gitlab_status", "expected_status", "expected_heading"),
    [
        ("failed", "failed", "❌ Failed"),
        ("canceled", "canceled", "🚫 Cancelled"),
        ("cancelled", "canceled", "🚫 Cancelled"),
        ("skipped", "skipped", "⏭️ Skipped"),
        ("timed_out", "timeout", "⏱️ Timed out"),
    ],
)
def test_terminal_pipeline_results_are_normalized(
    gitlab_status: str,
    expected_status: str,
    expected_heading: str,
) -> None:
    gitlab = FakeGitLabClient(pipeline_updates=[_pipeline(gitlab_status)])

    result = _runner(gitlab=gitlab).run()

    assert result.status == expected_status
    assert result.exit_code == 1
    assert f"**Status:** {expected_heading}" in result.summary
    assert gitlab.cancel_pipeline_ids == []


def test_pipeline_timeout_uses_injected_clock_and_still_collects_jobs() -> None:
    job = _job()
    gitlab = FakeGitLabClient(pipeline_updates=[_pipeline("running")], jobs=(job,))
    clock = FakeClock()

    result = _runner(gitlab=gitlab, clock=clock).run()

    assert result.status == "timeout"
    assert result.exit_code == 1
    assert result.jobs == (job,)
    assert result.pipeline == _pipeline("canceled")
    assert gitlab.cancel_pipeline_ids == [42]
    assert gitlab.cancel_timeouts == [10.0]
    assert clock.sleeps == [2.0, 2.0, 1.0]
    assert [call for call in gitlab.calls if call[0] == "get_pipeline"] == [
        ("get_pipeline", 42),
        ("get_pipeline", 42),
        ("get_pipeline", 42),
        ("get_pipeline", 42),
    ]


def test_pipeline_state_is_active_before_first_poll() -> None:
    state_store = RecordingStateStore()
    gitlab = FakeGitLabClient(pipeline_updates=[_pipeline("success")])

    def assert_active_state_exists() -> None:
        assert state_store.states[-1].active is True
        assert state_store.states[-1].pipeline_id == 42

    gitlab.hooks["get_pipeline"] = assert_active_state_exists

    _runner(gitlab=gitlab, state_store=state_store).run()

    assert state_store.states[0].pipeline_status == "pending"


@pytest.mark.parametrize("terminal_status", ["success", "failed", "canceled", "skipped"])
def test_terminal_pipeline_marks_persisted_state_inactive(terminal_status: str) -> None:
    state_store = RecordingStateStore()
    gitlab = FakeGitLabClient(pipeline_updates=[_pipeline(terminal_status)])

    _runner(gitlab=gitlab, state_store=state_store).run()

    assert state_store.states[-1].active is False
    assert state_store.states[-1].pipeline_status == terminal_status


def test_successful_timeout_cancellation_marks_state_inactive() -> None:
    state_store = RecordingStateStore()
    gitlab = FakeGitLabClient(pipeline_updates=[_pipeline("running")])

    result = _runner(gitlab=gitlab, state_store=state_store).run()

    assert result.status == "timeout"
    assert gitlab.cancel_pipeline_ids == [42]
    assert state_store.states[-1].active is False
    assert state_store.states[-1].pipeline_status == "canceled"


def test_failed_timeout_cancellation_leaves_state_active() -> None:
    state_store = RecordingStateStore()
    gitlab = FakeGitLabClient(pipeline_updates=[_pipeline("running")])
    gitlab.errors["cancel"] = RuntimeError(f"cancel failed {DUMMY_GITLAB_TOKEN}")

    result = _runner(gitlab=gitlab, state_store=state_store).run()

    assert result.status == "timeout"
    assert result.error_message == "GitLab pipeline timed out, but cleanup failed."
    assert gitlab.cancel_pipeline_ids == [42]
    assert state_store.states[-1].active is True
    assert DUMMY_GITLAB_TOKEN not in result.error_message
    assert DUMMY_GITLAB_TOKEN not in result.summary


def test_signal_during_timeout_cleanup_does_not_duplicate_cancel_request() -> None:
    cancellation = FakeCancellationController()
    gitlab = FakeGitLabClient(pipeline_updates=[_pipeline("running")])
    gitlab.hooks["cancel"] = cancellation.request_cancellation

    result = _runner(gitlab=gitlab, cancellation=cancellation).run()

    assert result.status == "timeout"
    assert gitlab.cancel_pipeline_ids == [42]


def test_timeout_accepts_canceling_response_and_marks_state_inactive() -> None:
    state_store = RecordingStateStore()
    gitlab = FakeGitLabClient(
        pipeline_updates=[_pipeline("running")],
        cancel_result=_pipeline("canceling"),
    )

    result = _runner(gitlab=gitlab, state_store=state_store).run()

    assert result.status == "timeout"
    assert result.pipeline == _pipeline("canceling")
    assert gitlab.cancel_pipeline_ids == [42]
    assert state_store.states[-1].active is False
    assert state_store.states[-1].pipeline_status == "canceling"


def test_no_jobs_produces_clear_fallback_without_trace_request() -> None:
    gitlab = FakeGitLabClient(pipeline_updates=[_pipeline("success")], jobs=())

    result = _runner(gitlab=gitlab).run()

    assert result.status == "success"
    assert result.selected_job is None
    assert result.processed_log.is_unavailable is True
    assert "No GitLab jobs were available" in result.summary
    assert all(call[0] != "get_trace" for call in gitlab.calls)


def test_job_trace_failure_becomes_unavailable_log_without_failing_pipeline() -> None:
    gitlab = FakeGitLabClient(
        pipeline_updates=[_pipeline("success")],
        jobs=(_job(),),
    )
    gitlab.errors["get_trace"] = RuntimeError("dummy trace failure")

    result = _runner(gitlab=gitlab).run()

    assert result.status == "success"
    assert result.processed_log.is_unavailable is True
    assert "job log is unavailable" in result.summary


@pytest.mark.parametrize(
    ("failing_operation", "expected_message"),
    [
        ("push", "github2lab failed while pushing the GitHub commit to GitLab."),
        ("trigger", "github2lab failed while triggering the GitLab pipeline."),
        ("get_pipeline", "github2lab failed while waiting for the GitLab pipeline."),
        ("get_jobs", "github2lab failed while fetching GitLab pipeline jobs."),
    ],
)
def test_worker_failures_return_sanitized_error(
    failing_operation: str,
    expected_message: str,
) -> None:
    secret_failure = RuntimeError(f"failure exposed {DUMMY_GITLAB_TOKEN}")
    pusher = FakeGitPusher(error=secret_failure if failing_operation == "push" else None)
    gitlab = FakeGitLabClient(pipeline_updates=[_pipeline("success")])
    if failing_operation != "push":
        gitlab.errors[failing_operation] = secret_failure

    result = _runner(pusher=pusher, gitlab=gitlab).run()

    assert result.status == "error"
    assert result.exit_code == 1
    assert result.error_message == expected_message
    assert DUMMY_GITLAB_TOKEN not in result.error_message
    assert DUMMY_GITLAB_TOKEN not in result.summary
    assert DUMMY_GITLAB_TOKEN not in repr(result)


def test_cancellation_before_push_runs_no_external_operation() -> None:
    cancellation = FakeCancellationController()
    cancellation.request_cancellation()
    pusher = FakeGitPusher()
    gitlab = FakeGitLabClient()

    result = _runner(pusher=pusher, gitlab=gitlab, cancellation=cancellation).run()

    assert result.status == "canceled"
    assert pusher.calls == 0
    assert gitlab.calls == []
    assert result.processed_log.is_unavailable is True
    assert "cancelled before log collection completed" in result.processed_log.text


def test_cancellation_after_branch_push_does_not_trigger_pipeline() -> None:
    cancellation = FakeCancellationController()
    pusher = FakeGitPusher(hook=cancellation.request_cancellation)
    gitlab = FakeGitLabClient()

    result = _runner(pusher=pusher, gitlab=gitlab, cancellation=cancellation).run()

    assert result.status == "canceled"
    assert pusher.calls == 1
    assert gitlab.calls == []


def test_cancellation_during_trigger_cancels_newly_known_pipeline_once() -> None:
    cancellation = FakeCancellationController()
    gitlab = FakeGitLabClient()
    gitlab.hooks["trigger"] = cancellation.request_cancellation

    result = _runner(gitlab=gitlab, cancellation=cancellation).run()

    assert result.status == "canceled"
    assert result.pipeline == _pipeline("canceled")
    assert gitlab.cancel_pipeline_ids == [42]


def test_manual_cancellation_accepts_canceling_response() -> None:
    cancellation = FakeCancellationController()
    state_store = RecordingStateStore()
    gitlab = FakeGitLabClient(cancel_result=_pipeline("canceling"))
    gitlab.hooks["trigger"] = cancellation.request_cancellation

    result = _runner(
        gitlab=gitlab,
        cancellation=cancellation,
        state_store=state_store,
    ).run()

    assert result.status == "canceled"
    assert result.pipeline == _pipeline("canceling")
    assert gitlab.cancel_pipeline_ids == [42]
    assert state_store.states[-1].active is False
    assert state_store.states[-1].pipeline_status == "canceling"


def test_duplicate_cancellation_during_polling_cancels_pipeline_once() -> None:
    cancellation = FakeCancellationController()
    gitlab = FakeGitLabClient(pipeline_updates=[_pipeline("running")])

    def request_twice() -> None:
        cancellation.request_cancellation()
        cancellation.request_cancellation()

    gitlab.hooks["get_pipeline"] = request_twice

    result = _runner(gitlab=gitlab, cancellation=cancellation).run()

    assert result.status == "canceled"
    assert result.pipeline == _pipeline("canceled")
    assert gitlab.cancel_pipeline_ids == [42]


def test_cancellation_cleanup_failure_is_sanitized_and_not_retried() -> None:
    cancellation = FakeCancellationController()
    gitlab = FakeGitLabClient()
    gitlab.hooks["trigger"] = cancellation.request_cancellation
    gitlab.errors["cancel"] = RuntimeError(f"cancel failed {DUMMY_GITLAB_TOKEN}")

    result = _runner(gitlab=gitlab, cancellation=cancellation).run()

    assert result.status == "canceled"
    assert result.error_message == "Cancellation cleanup failed."
    assert gitlab.cancel_pipeline_ids == [42]
    assert DUMMY_GITLAB_TOKEN not in result.error_message


@pytest.mark.parametrize("operation", ["get_jobs", "get_trace"])
def test_cancellation_during_result_collection_is_safe(operation: str) -> None:
    cancellation = FakeCancellationController()
    gitlab = FakeGitLabClient(
        pipeline_updates=[_pipeline("success")],
        jobs=(_job(),),
        trace=b"complete\n",
    )
    gitlab.hooks[operation] = cancellation.request_cancellation

    result = _runner(gitlab=gitlab, cancellation=cancellation).run()

    assert result.status == "canceled"
    assert gitlab.cancel_pipeline_ids == []
    assert "**Status:** 🚫 Cancelled" in result.summary


def test_cancellation_during_summary_rendering_updates_summary() -> None:
    cancellation = FakeCancellationController()
    gitlab = FakeGitLabClient(pipeline_updates=[_pipeline("success")])

    def canceling_renderer(data: GitHubSummaryData) -> str:
        cancellation.request_cancellation()
        return render_github_summary(data)

    result = _runner(
        gitlab=gitlab,
        cancellation=cancellation,
        renderer=canceling_renderer,
    ).run()

    assert result.status == "canceled"
    assert "**Status:** 🚫 Cancelled" in result.summary


def test_cancellation_during_summary_write_updates_final_result() -> None:
    cancellation = FakeCancellationController()
    writer = FakeSummaryWriter(hook=cancellation.request_cancellation)
    gitlab = FakeGitLabClient(pipeline_updates=[_pipeline("success")])

    result = _runner(
        gitlab=gitlab,
        cancellation=cancellation,
        writer=writer,
    ).run()

    assert result.status == "canceled"
    assert "**Status:** 🚫 Cancelled" in result.summary
    assert gitlab.cancel_pipeline_ids == []


def test_summary_writer_failure_returns_error_without_exception_details() -> None:
    writer = FakeSummaryWriter(error=RuntimeError(f"write failed {DUMMY_GITLAB_TOKEN}"))

    result = _runner(writer=writer).run()

    assert result.status == "error"
    assert result.error_message == "Unable to write the GitHub Actions summary."
    assert "**Status:** ❓ Unknown" in result.summary
    assert DUMMY_GITLAB_TOKEN not in result.error_message
    assert DUMMY_GITLAB_TOKEN not in result.summary


def test_summary_renderer_failure_returns_minimal_fallback() -> None:
    def failing_renderer(data: GitHubSummaryData) -> str:
        del data
        raise RuntimeError(f"render failed {DUMMY_TRIGGER_TOKEN}")

    result = _runner(renderer=failing_renderer).run()

    assert result.status == "error"
    assert result.error_message == "Unable to generate the GitHub Actions summary."
    assert "summary could not be generated" in result.summary
    assert DUMMY_TRIGGER_TOKEN not in result.summary


def test_runner_instance_can_only_run_once() -> None:
    runner = _runner()
    runner.run()

    with pytest.raises(RunnerError, match="only run once"):
        runner.run()


def test_runner_repr_does_not_expose_configuration_secrets() -> None:
    representation = repr(_runner())

    assert representation == "GitHub2LabRunner()"
    assert DUMMY_GITLAB_TOKEN not in representation
    assert DUMMY_TRIGGER_TOKEN not in representation


def test_select_log_job_returns_only_single_job() -> None:
    first_job = _job(101, "first")
    second_job = _job(102, "second")

    assert select_log_job((first_job,)) == first_job
    assert select_log_job((first_job, second_job)) is None
    assert select_log_job(()) is None


def test_file_summary_writer_appends_utf8_markdown(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.md"
    writer = FileSummaryWriter(summary_path)

    writer.write("first ✅\n")
    writer.write("second\n")

    assert summary_path.read_text(encoding="utf-8") == "first ✅\nsecond\n"
    assert repr(writer) == "FileSummaryWriter()"


def test_file_summary_writer_sanitizes_file_errors() -> None:
    writer = FileSummaryWriter("/dummy/summary.md")

    with patch("pathlib.Path.open", side_effect=OSError(f"denied {DUMMY_GITLAB_TOKEN}")):
        with pytest.raises(SummaryWriteError) as captured:
            writer.write("summary")

    assert str(captured.value) == "Unable to write the GitHub Actions summary."
    assert DUMMY_GITLAB_TOKEN not in str(captured.value)
