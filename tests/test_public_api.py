# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Tests for the public ClusterHop Python API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import clusterhop
from clusterhop import RunConfigurationError, RunResult, UnsupportedDestinationError

DUMMY_TOKEN = "dummy-private-token-that-must-not-appear"
DUMMY_TRIGGER_TOKEN = "dummy-trigger-token-that-must-not-appear"


def _config(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "destination_type": "gitlab",
        "gitlab_url": "https://gitlab.example.test",
        "gitlab_remote_url": "git@gitlab.example.test:group/project.git",
        "gitlab_project_id": "1234",
        "github_commit_sha": "0123456789abcdef0123456789abcdef01234567",
        "github_repository": "example/source-project",
        "github_run_id": "987654",
    }
    values.update(overrides)
    return values


def _secrets(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "gitlab_token": DUMMY_TOKEN,
        "gitlab_trigger_token": DUMMY_TRIGGER_TOKEN,
    }
    values.update(overrides)
    return values


def _internal_result() -> SimpleNamespace:
    return SimpleNamespace(
        status="success",
        exit_code=0,
        summary="## GitLab CI Result\n",
        error_message=None,
    )


def test_run_is_importable_from_top_level_package() -> None:
    assert callable(clusterhop.run)


def test_run_adapts_gitlab_inputs_to_existing_runner() -> None:
    options = {
        "poll_interval_seconds": 2.5,
        "pipeline_timeout_seconds": 60,
        "cleanup_timeout_seconds": 8,
        "pipeline_state_file": Path("pipeline-state.json"),
    }

    with (
        patch.dict("os.environ", {}, clear=True),
        patch("clusterhop.api.run_from_environment", return_value=_internal_result()) as backend,
    ):
        result = clusterhop.run(
            config=_config(
                extra_pipeline_variables={
                    "CUSTOM_MODE": "validation",
                    "EMPTY_VALUE": "",
                }
            ),
            repo_path=Path("repository"),
            options=options,
            secrets=_secrets(),
        )

    environment = backend.call_args.args[0]
    assert environment == {
        "GITLAB_TOKEN": DUMMY_TOKEN,
        "GITLAB_TRIGGER_TOKEN": DUMMY_TRIGGER_TOKEN,
        "GITLAB_URL": "https://gitlab.example.test",
        "GITLAB_REMOTE_URL": "git@gitlab.example.test:group/project.git",
        "GITLAB_PROJECT_ID": "1234",
        "GITHUB_SHA": "0123456789abcdef0123456789abcdef01234567",
        "GITHUB_REPOSITORY": "example/source-project",
        "GITHUB_RUN_ID": "987654",
        "CLUSTERHOP_POLL_INTERVAL_SECONDS": "2.5",
        "CLUSTERHOP_PIPELINE_TIMEOUT_SECONDS": "60",
        "CLUSTERHOP_CLEANUP_TIMEOUT_SECONDS": "8",
        "CLUSTERHOP_PIPELINE_STATE_FILE": "pipeline-state.json",
        "CLUSTERHOP_GITLAB_VARIABLES": json.dumps(
            {"CUSTOM_MODE": "validation", "EMPTY_VALUE": ""},
            separators=(",", ":"),
            sort_keys=True,
        ),
    }
    assert backend.call_args.kwargs == {"repository_path": Path("repository")}
    assert result == RunResult(
        status="success",
        exit_code=0,
        summary="## GitLab CI Result\n",
    )


def test_run_does_not_map_log_summary_limit_from_options() -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("clusterhop.api.run_from_environment", return_value=_internal_result()) as backend,
    ):
        clusterhop.run(
            config=_config(),
            repo_path=".",
            options={"log_summary_limit_bytes": 100_000},
            secrets=_secrets(),
        )

    environment = backend.call_args.args[0]
    assert "CLUSTERHOP_LOG_SUMMARY_LIMIT_BYTES" not in environment


def test_run_preserves_environment_log_summary_limit() -> None:
    with (
        patch.dict(
            "os.environ",
            {"CLUSTERHOP_LOG_SUMMARY_LIMIT_BYTES": "123456"},
            clear=True,
        ),
        patch("clusterhop.api.run_from_environment", return_value=_internal_result()) as backend,
    ):
        clusterhop.run(
            config=_config(),
            repo_path=".",
            secrets=_secrets(),
        )

    environment = backend.call_args.args[0]
    assert environment["CLUSTERHOP_LOG_SUMMARY_LIMIT_BYTES"] == "123456"


def test_run_omits_pipeline_variables_environment_value_when_not_configured() -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("clusterhop.api.run_from_environment", return_value=_internal_result()) as backend,
    ):
        clusterhop.run(config=_config(), repo_path=".", secrets=_secrets())

    assert "CLUSTERHOP_GITLAB_VARIABLES" not in backend.call_args.args[0]


@pytest.mark.parametrize(
    "pipeline_variables",
    [
        [],
        {1: "value"},
        {"NAME": 1},
        {"INVALID-NAME": "value"},
        {"1INVALID": "value"},
        {"GITHUB_COMMIT_SHA": "replacement"},
        {"GITHUB_REPOSITORY": "replacement"},
        {"GITHUB_RUN_ID": "replacement"},
    ],
)
def test_run_rejects_invalid_extra_pipeline_variables(pipeline_variables: object) -> None:
    with patch("clusterhop.api.run_from_environment") as backend:
        with pytest.raises(RunConfigurationError, match="extra_pipeline_variables"):
            clusterhop.run(
                config=_config(extra_pipeline_variables=pipeline_variables),
                repo_path=".",
                secrets=_secrets(),
            )

    backend.assert_not_called()


def test_pipeline_variable_validation_does_not_expose_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_value = "dummy-pipeline-secret-that-must-not-appear"

    with pytest.raises(RunConfigurationError) as captured:
        clusterhop.run(
            config=_config(extra_pipeline_variables={"INVALID-NAME": secret_value}),
            repo_path=".",
            secrets=_secrets(),
        )

    assert secret_value not in str(captured.value)
    output = capsys.readouterr()
    assert secret_value not in output.out
    assert secret_value not in output.err


@dataclass
class ObjectConfig:
    type: str = "gitlab"
    gitlab_url: str = "https://gitlab.example.test"
    gitlab_remote_url: str = "git@gitlab.example.test:group/project.git"
    gitlab_project_path: str = "group/project"
    github_commit_sha: str = "0123456789abcdef0123456789abcdef01234567"
    github_repository: str = "example/source-project"
    github_run_id: str = "987654"


@dataclass
class ObjectSecrets:
    gitlab_token: str = DUMMY_TOKEN
    gitlab_trigger_token: str = DUMMY_TRIGGER_TOKEN


def test_run_accepts_already_loaded_objects() -> None:
    with patch("clusterhop.api.run_from_environment", return_value=_internal_result()) as backend:
        clusterhop.run(
            config=ObjectConfig(),
            repo_path=".",
            options=SimpleNamespace(poll_interval_seconds=3),
            secrets=ObjectSecrets(),
        )

    environment = backend.call_args.args[0]
    assert environment["GITLAB_PROJECT_PATH"] == "group/project"
    assert environment["CLUSTERHOP_POLL_INTERVAL_SECONDS"] == "3"


@pytest.mark.parametrize(
    ("project_fields", "expected_environment"),
    [
        ({"gitlab_project_id": 8894}, {"GITLAB_PROJECT_ID": "8894"}),
        ({"gitlab_project_path": "group/project"}, {"GITLAB_PROJECT_PATH": "group/project"}),
    ],
)
def test_run_accepts_flat_project_id_or_path(
    project_fields: dict[str, object],
    expected_environment: dict[str, str],
) -> None:
    config = _config()
    config.pop("gitlab_project_id")
    config.update(project_fields)

    with patch("clusterhop.api.run_from_environment", return_value=_internal_result()) as backend:
        clusterhop.run(config=config, repo_path=".", secrets=_secrets())

    environment = backend.call_args.args[0]
    for name, value in expected_environment.items():
        assert environment[name] == value
    assert not ({"GITLAB_PROJECT_ID", "GITLAB_PROJECT_PATH"} - expected_environment.keys()) & set(
        environment
    )


@pytest.mark.parametrize("destination_type", ["github", "forgejo", "unknown"])
def test_run_rejects_unsupported_destination(destination_type: str) -> None:
    with patch("clusterhop.api.run_from_environment") as backend:
        with pytest.raises(UnsupportedDestinationError, match="not supported"):
            clusterhop.run(
                config=_config(destination_type=destination_type),
                repo_path=".",
                secrets=_secrets(),
            )

    backend.assert_not_called()


@pytest.mark.parametrize(
    ("config", "secrets", "expected_field"),
    [
        ({}, _secrets(), "destination_type"),
        (_config(gitlab_url=None), _secrets(), "GITLAB_URL"),
        (_config(), {}, "gitlab_token"),
        (_config(), {"gitlab_token": DUMMY_TOKEN}, "gitlab_trigger_token"),
    ],
)
def test_run_rejects_missing_required_inputs_without_exposing_secrets(
    config: dict[str, object],
    secrets: dict[str, object],
    expected_field: str,
) -> None:
    with pytest.raises(RunConfigurationError) as captured:
        clusterhop.run(config=config, repo_path=".", secrets=secrets)

    message = str(captured.value)
    assert expected_field in message
    assert DUMMY_TOKEN not in message
    assert DUMMY_TRIGGER_TOKEN not in message


def test_public_result_repr_does_not_include_summary_or_error_details() -> None:
    result = RunResult(
        status="error",
        exit_code=1,
        summary=f"summary {DUMMY_TOKEN}",
        error_message=f"error {DUMMY_TRIGGER_TOKEN}",
    )

    representation = repr(result)
    assert DUMMY_TOKEN not in representation
    assert DUMMY_TRIGGER_TOKEN not in representation
    assert representation == "RunResult(status='error', exit_code=1)"


def test_run_does_not_print_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    backend = Mock(return_value=_internal_result())
    with patch("clusterhop.api.run_from_environment", backend):
        clusterhop.run(config=_config(), repo_path=".", secrets=_secrets())

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
