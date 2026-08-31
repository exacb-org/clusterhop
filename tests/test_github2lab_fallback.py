# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Tests for workflow-level fallback GitLab pipeline cancellation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from clusterhop.github2lab.config import GitHub2LabConfig
from clusterhop.github2lab.fallback import (
    FallbackCancellationError,
    FallbackCancellationResult,
    cancel_pipeline_from_state,
    cancel_pipeline_from_state_environment,
    main,
)
from clusterhop.github2lab.models import GitLabPipeline
from clusterhop.github2lab.state import PipelineState

DUMMY_TOKEN = "dummy-token-that-must-remain-secret"
DUMMY_TRIGGER_TOKEN = "dummy-trigger-that-must-remain-secret"


def _config(**overrides: object) -> GitHub2LabConfig:
    values: dict[str, object] = {
        "gitlab_token": DUMMY_TOKEN,
        "gitlab_trigger_token": DUMMY_TRIGGER_TOKEN,
        "gitlab_url": "https://gitlab.example.test",
        "gitlab_remote_url": "https://gitlab.example.test/group/project.git",
        "gitlab_project_id": "1234",
        "gitlab_project_path": None,
        "github_commit_sha": "0123456789abcdef0123456789abcdef01234567",
        "github_repository": "example/project",
        "github_run_id": "987654",
        "cleanup_timeout_seconds": 7.0,
    }
    values.update(overrides)
    return GitHub2LabConfig(**values)  # type: ignore[arg-type]


def _pipeline(status: str) -> GitLabPipeline:
    return GitLabPipeline(
        id=42,
        status=status,
        web_url="https://gitlab.example.test/group/project/-/pipelines/42",
        ref="ci/0123456789abcdef0123456789abcdef01234567",
        sha="0123456789abcdef0123456789abcdef01234567",
    )


def _state(*, active: bool = True, status: str = "running", **overrides: object) -> PipelineState:
    values: dict[str, object] = {
        "active": active,
        "pipeline_id": 42,
        "pipeline_status": status,
        "gitlab_ref": "ci/0123456789abcdef0123456789abcdef01234567",
        "project_identifier": "id:1234",
        "github_run_id": "987654",
    }
    values.update(overrides)
    return PipelineState(**values)  # type: ignore[arg-type]


class MemoryStateStore:
    def __init__(self, state: PipelineState | None) -> None:
        self.state = state
        self.writes: list[PipelineState] = []

    def read(self) -> PipelineState | None:
        return self.state

    def write(self, state: PipelineState) -> None:
        self.state = state
        self.writes.append(state)


class FakeGitLab:
    def __init__(
        self,
        *,
        current: GitLabPipeline | None = None,
        canceled: GitLabPipeline | None = None,
        error: Exception | None = None,
    ) -> None:
        self.current = current or _pipeline("running")
        self.canceled = canceled or _pipeline("canceled")
        self.error = error
        self.get_calls: list[int] = []
        self.cancel_calls: list[tuple[int, float | None]] = []

    def get_pipeline(self, pipeline_id: int) -> GitLabPipeline:
        self.get_calls.append(pipeline_id)
        if self.error is not None:
            raise self.error
        return self.current

    def cancel_pipeline(
        self,
        pipeline_id: int,
        *,
        timeout_seconds: float | None = None,
    ) -> GitLabPipeline:
        self.cancel_calls.append((pipeline_id, timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.canceled


def test_missing_state_is_successful_no_op() -> None:
    store = MemoryStateStore(None)
    gitlab = FakeGitLab()

    result = cancel_pipeline_from_state(_config(), store, gitlab)

    assert result.action == "no_state"
    assert gitlab.get_calls == []
    assert gitlab.cancel_calls == []


def test_environment_fallback_missing_file_needs_no_secrets(tmp_path: Path) -> None:
    result = cancel_pipeline_from_state_environment(
        {"CLUSTERHOP_PIPELINE_STATE_FILE": str(tmp_path / "missing.json")}
    )

    assert result.action == "no_state"


def test_environment_fallback_rejects_blank_state_path() -> None:
    with pytest.raises(FallbackCancellationError, match="state path is invalid"):
        cancel_pipeline_from_state_environment({"CLUSTERHOP_PIPELINE_STATE_FILE": "   "})


def test_inactive_state_is_successful_no_op() -> None:
    store = MemoryStateStore(_state(active=False, status="success"))
    gitlab = FakeGitLab()

    result = cancel_pipeline_from_state(_config(), store, gitlab)

    assert result.action == "already_inactive"
    assert gitlab.get_calls == []
    assert gitlab.cancel_calls == []


def test_active_pipeline_is_canceled_once_with_cleanup_timeout() -> None:
    store = MemoryStateStore(_state())
    gitlab = FakeGitLab()

    first_result = cancel_pipeline_from_state(_config(), store, gitlab)
    second_result = cancel_pipeline_from_state(_config(), store, gitlab)

    assert first_result.action == "canceled"
    assert second_result.action == "already_inactive"
    assert gitlab.get_calls == [42]
    assert gitlab.cancel_calls == [(42, 7.0)]
    assert store.state is not None
    assert store.state.active is False
    assert store.state.pipeline_status == "canceled"


def test_canceling_response_is_accepted_and_repeated_fallback_is_safe() -> None:
    store = MemoryStateStore(_state())
    gitlab = FakeGitLab(canceled=_pipeline("canceling"))

    first_result = cancel_pipeline_from_state(_config(), store, gitlab)
    second_result = cancel_pipeline_from_state(_config(), store, gitlab)

    assert first_result.action == "cancellation_accepted"
    assert second_result.action == "already_inactive"
    assert gitlab.get_calls == [42]
    assert gitlab.cancel_calls == [(42, 7.0)]
    assert store.state is not None
    assert store.state.active is False
    assert store.state.pipeline_status == "canceling"


def test_already_canceling_pipeline_is_marked_inactive_without_duplicate_cancel() -> None:
    store = MemoryStateStore(_state())
    gitlab = FakeGitLab(current=_pipeline("canceling"))

    result = cancel_pipeline_from_state(_config(), store, gitlab)

    assert result.action == "cancellation_accepted"
    assert gitlab.get_calls == [42]
    assert gitlab.cancel_calls == []
    assert store.state is not None
    assert store.state.active is False
    assert store.state.pipeline_status == "canceling"


def test_command_exits_successfully_when_cancellation_is_accepted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch(
        "clusterhop.github2lab.fallback.cancel_pipeline_from_state_environment",
        return_value=FallbackCancellationResult("cancellation_accepted", 42),
    ):
        exit_code = main()

    assert exit_code == 0
    assert "accepted" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["success", "failed", "canceled", "skipped"])
def test_already_terminal_pipeline_is_not_canceled(status: str) -> None:
    store = MemoryStateStore(_state())
    gitlab = FakeGitLab(current=_pipeline(status))

    result = cancel_pipeline_from_state(_config(), store, gitlab)

    assert result.action == "already_terminal"
    assert gitlab.cancel_calls == []
    assert store.state is not None
    assert store.state.active is False
    assert store.state.pipeline_status == status


@pytest.mark.parametrize(
    "state",
    [
        _state(github_run_id="different-run"),
        _state(project_identifier="id:9999"),
    ],
)
def test_mismatched_state_fails_without_api_calls(state: PipelineState) -> None:
    store = MemoryStateStore(state)
    gitlab = FakeGitLab()

    with pytest.raises(FallbackCancellationError, match="does not match"):
        cancel_pipeline_from_state(_config(), store, gitlab)

    assert gitlab.get_calls == []
    assert gitlab.cancel_calls == []


def test_failed_fallback_leaves_state_active_and_hides_secrets() -> None:
    store = MemoryStateStore(_state())
    gitlab = FakeGitLab(error=RuntimeError(f"failed with {DUMMY_TOKEN}"))

    with pytest.raises(FallbackCancellationError) as captured:
        cancel_pipeline_from_state(_config(), store, gitlab)

    assert store.state is not None
    assert store.state.active is True
    assert DUMMY_TOKEN not in str(captured.value)
    assert DUMMY_TRIGGER_TOKEN not in str(captured.value)
    assert DUMMY_TOKEN not in repr(captured.value)


def test_unconfirmed_cancel_response_leaves_state_active() -> None:
    store = MemoryStateStore(_state())
    gitlab = FakeGitLab(canceled=_pipeline("running"))

    with pytest.raises(FallbackCancellationError, match="did not confirm"):
        cancel_pipeline_from_state(_config(), store, gitlab)

    assert store.state is not None
    assert store.state.active is True


def test_corrupt_environment_state_fails_before_configuration_or_api(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(f"corrupt {DUMMY_TOKEN}", encoding="utf-8")

    with pytest.raises(FallbackCancellationError) as captured:
        cancel_pipeline_from_state_environment({"CLUSTERHOP_PIPELINE_STATE_FILE": str(path)})

    assert str(captured.value) == "Fallback pipeline state is invalid."
    assert DUMMY_TOKEN not in str(captured.value)
