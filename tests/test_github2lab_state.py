# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Tests for persistent GitLab pipeline lifecycle state."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from clusterhop.github2lab.config import GitHub2LabConfig
from clusterhop.github2lab.models import GitLabPipeline
from clusterhop.github2lab.state import (
    PIPELINE_STATE_SCHEMA_VERSION,
    FilePipelineStateStore,
    PipelineState,
    PipelineStateError,
    is_cancellation_accepted_status,
    pipeline_state_from_config,
)

DUMMY_TOKEN = "dummy-token-that-must-not-be-written"
DUMMY_TRIGGER_TOKEN = "dummy-trigger-that-must-not-be-written"


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
    }
    values.update(overrides)
    return GitHub2LabConfig(**values)  # type: ignore[arg-type]


def _pipeline(status: str = "running") -> GitLabPipeline:
    return GitLabPipeline(
        id=42,
        status=status,
        web_url="https://gitlab.example.test/group/project/-/pipelines/42",
        ref="ci/0123456789abcdef0123456789abcdef01234567",
        sha="0123456789abcdef0123456789abcdef01234567",
    )


def _state(*, active: bool = True, status: str = "running") -> PipelineState:
    return pipeline_state_from_config(_config(), _pipeline(status), active=active)


def test_atomic_state_round_trip_uses_versioned_allowlisted_json(tmp_path: Path) -> None:
    path = tmp_path / "pipeline-state.json"
    store = FilePipelineStateStore(path)

    store.write(_state())

    assert store.read() == _state()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "active": True,
        "github_run_id": "987654",
        "gitlab_ref": "ci/0123456789abcdef0123456789abcdef01234567",
        "pipeline_id": 42,
        "pipeline_status": "running",
        "project_identifier": "id:1234",
        "schema_version": PIPELINE_STATE_SCHEMA_VERSION,
    }
    serialized = path.read_text(encoding="utf-8")
    assert DUMMY_TOKEN not in serialized
    assert DUMMY_TRIGGER_TOKEN not in serialized
    assert not list(tmp_path.glob(".pipeline-state.json.*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")
def test_state_file_permissions_are_restrictive(tmp_path: Path) -> None:
    path = tmp_path / "pipeline-state.json"

    FilePipelineStateStore(path).write(_state())

    assert path.stat().st_mode & 0o777 == 0o600


def test_atomic_replacement_preserves_previous_state_when_replace_fails(tmp_path: Path) -> None:
    path = tmp_path / "pipeline-state.json"
    store = FilePipelineStateStore(path)
    original = _state()
    store.write(original)

    with patch("clusterhop.github2lab.state.os.replace", side_effect=OSError("dummy")):
        with pytest.raises(PipelineStateError, match="Unable to write"):
            store.write(_state(active=False, status="success"))

    assert store.read() == original
    assert not list(tmp_path.glob(".pipeline-state.json.*.tmp"))


def test_missing_state_file_returns_none(tmp_path: Path) -> None:
    assert FilePipelineStateStore(tmp_path / "missing.json").read() is None


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "{}",
        '{"schema_version":999}',
        (
            '{"active":true,"github_run_id":"987654","gitlab_ref":"ci/ref",'
            '"pipeline_id":-1,"pipeline_status":"running",'
            '"project_identifier":"id:1234","schema_version":1}'
        ),
    ],
)
def test_corrupt_or_invalid_state_is_rejected_safely(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "pipeline-state.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(PipelineStateError) as captured:
        FilePipelineStateStore(path).read()

    assert DUMMY_TOKEN not in str(captured.value)
    assert payload not in str(captured.value)


def test_inactive_state_round_trip(tmp_path: Path) -> None:
    state = _state(active=False, status="success")
    store = FilePipelineStateStore(tmp_path / "pipeline-state.json")

    store.write(state)

    assert store.read() == state


def test_state_store_repr_hides_path(tmp_path: Path) -> None:
    secret_path = tmp_path / DUMMY_TOKEN

    representation = repr(FilePipelineStateStore(secret_path))

    assert representation == "FilePipelineStateStore()"
    assert DUMMY_TOKEN not in representation


def test_project_path_identity_is_unambiguous() -> None:
    config = _config(gitlab_project_id=None, gitlab_project_path="group/project")

    state = pipeline_state_from_config(config, _pipeline(), active=True)

    assert state.project_identifier == "path:group/project"


@pytest.mark.parametrize("status", ["canceling", "canceled", "cancelled", " CANCELING "])
def test_cancellation_accepted_statuses(status: str) -> None:
    assert is_cancellation_accepted_status(status) is True


def test_running_is_not_a_cancellation_accepted_status() -> None:
    assert is_cancellation_accepted_status("running") is False
