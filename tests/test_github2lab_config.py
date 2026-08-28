# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Tests for GitHub-to-GitLab configuration loading and validation."""

from pathlib import Path

import pytest

from clusterhop.github2lab.config import (
    DEFAULT_LOG_SUMMARY_LIMIT_BYTES,
    DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    DEFAULT_PIPELINE_TIMEOUT_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    ConfigurationError,
    GitHub2LabConfig,
    load_config_from_environment,
)

GITLAB_TOKEN = "dummy-gitlab-token"
GITLAB_TRIGGER_TOKEN = "dummy-trigger-token"


def valid_environment() -> dict[str, str]:
    """Return an isolated environment mapping with fake valid values."""

    return {
        "GITLAB_TOKEN": GITLAB_TOKEN,
        "GITLAB_TRIGGER_TOKEN": GITLAB_TRIGGER_TOKEN,
        "GITLAB_URL": "https://gitlab.example.test",
        "GITLAB_REMOTE_URL": "https://gitlab.example.test/group/project.git",
        "GITLAB_PROJECT_ID": "1234",
        "GITHUB_SHA": "0123456789abcdef0123456789abcdef01234567",
        "GITHUB_REPOSITORY": "example/project",
        "GITHUB_RUN_ID": "987654321",
    }


def valid_config(**overrides: object) -> GitHub2LabConfig:
    """Construct a valid configuration, applying selected field overrides."""

    values: dict[str, object] = {
        "gitlab_token": GITLAB_TOKEN,
        "gitlab_trigger_token": GITLAB_TRIGGER_TOKEN,
        "gitlab_url": "https://gitlab.example.test",
        "gitlab_remote_url": "https://gitlab.example.test/group/project.git",
        "gitlab_project_id": "1234",
        "gitlab_project_path": None,
        "github_commit_sha": "0123456789abcdef0123456789abcdef01234567",
        "github_repository": "example/project",
        "github_run_id": "987654321",
    }
    values.update(overrides)
    return GitHub2LabConfig(**values)  # type: ignore[arg-type]


def test_loads_project_id_and_prototype_defaults() -> None:
    config = load_config_from_environment(valid_environment())

    assert config.gitlab_project_id == "1234"
    assert config.gitlab_project_path is None
    assert config.poll_interval_seconds == DEFAULT_POLL_INTERVAL_SECONDS == 5.0
    assert config.pipeline_timeout_seconds == DEFAULT_PIPELINE_TIMEOUT_SECONDS == 150.0
    assert config.log_summary_limit_bytes == DEFAULT_LOG_SUMMARY_LIMIT_BYTES == 800_000
    assert config.extra_pipeline_variables == {}
    assert config.cleanup_timeout_seconds == DEFAULT_CLEANUP_TIMEOUT_SECONDS == 10.0
    assert config.pipeline_state_file is None


def test_loads_project_path_instead_of_project_id() -> None:
    environ = valid_environment()
    del environ["GITLAB_PROJECT_ID"]
    environ["GITLAB_PROJECT_PATH"] = "group/project"

    config = load_config_from_environment(environ)

    assert config.gitlab_project_id is None
    assert config.gitlab_project_path == "group/project"


def test_loads_runtime_overrides() -> None:
    environ = valid_environment()
    environ.update(
        {
            "CLUSTERHOP_POLL_INTERVAL_SECONDS": "2.5",
            "CLUSTERHOP_PIPELINE_TIMEOUT_SECONDS": "300",
            "CLUSTERHOP_LOG_SUMMARY_LIMIT_BYTES": "1000000",
            "CLUSTERHOP_CLEANUP_TIMEOUT_SECONDS": "7.5",
            "CLUSTERHOP_PIPELINE_STATE_FILE": "/tmp/github2lab-state.json",
        }
    )

    config = load_config_from_environment(environ)

    assert config.poll_interval_seconds == 2.5
    assert config.pipeline_timeout_seconds == 300.0
    assert config.log_summary_limit_bytes == 1_000_000
    assert config.cleanup_timeout_seconds == 7.5
    assert config.pipeline_state_file == Path("/tmp/github2lab-state.json")


@pytest.mark.parametrize(
    "environment_name",
    [
        "GITLAB_TOKEN",
        "GITLAB_TRIGGER_TOKEN",
        "GITLAB_URL",
        "GITLAB_REMOTE_URL",
        "GITHUB_SHA",
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ID",
    ],
)
def test_rejects_missing_required_environment_values(environment_name: str) -> None:
    environ = valid_environment()
    del environ[environment_name]

    with pytest.raises(ConfigurationError, match=environment_name):
        load_config_from_environment(environ)


def test_rejects_blank_required_environment_value() -> None:
    environ = valid_environment()
    environ["GITHUB_REPOSITORY"] = "   "

    with pytest.raises(ConfigurationError, match="GITHUB_REPOSITORY"):
        load_config_from_environment(environ)


def test_rejects_missing_project_id_and_path() -> None:
    environ = valid_environment()
    del environ["GITLAB_PROJECT_ID"]

    with pytest.raises(ConfigurationError, match="gitlab_project_id or gitlab_project_path"):
        load_config_from_environment(environ)


def test_rejects_project_id_and_path_together() -> None:
    environ = valid_environment()
    environ["GITLAB_PROJECT_PATH"] = "group/project"

    with pytest.raises(ConfigurationError, match="gitlab_project_id or gitlab_project_path"):
        load_config_from_environment(environ)


@pytest.mark.parametrize(
    ("environment_name", "value"),
    [
        ("CLUSTERHOP_POLL_INTERVAL_SECONDS", "not-a-number"),
        ("CLUSTERHOP_PIPELINE_TIMEOUT_SECONDS", "not-a-number"),
        ("CLUSTERHOP_LOG_SUMMARY_LIMIT_BYTES", "1.5"),
        ("CLUSTERHOP_CLEANUP_TIMEOUT_SECONDS", "not-a-number"),
    ],
)
def test_rejects_non_numeric_runtime_overrides(environment_name: str, value: str) -> None:
    environ = valid_environment()
    environ[environment_name] = value

    with pytest.raises(ConfigurationError, match=environment_name):
        load_config_from_environment(environ)


@pytest.mark.parametrize(
    ("environment_name", "value", "field_name"),
    [
        ("CLUSTERHOP_POLL_INTERVAL_SECONDS", "0", "poll_interval_seconds"),
        ("CLUSTERHOP_POLL_INTERVAL_SECONDS", "-1", "poll_interval_seconds"),
        ("CLUSTERHOP_PIPELINE_TIMEOUT_SECONDS", "0", "pipeline_timeout_seconds"),
        ("CLUSTERHOP_PIPELINE_TIMEOUT_SECONDS", "-1", "pipeline_timeout_seconds"),
        ("CLUSTERHOP_LOG_SUMMARY_LIMIT_BYTES", "0", "log_summary_limit_bytes"),
        ("CLUSTERHOP_LOG_SUMMARY_LIMIT_BYTES", "-1", "log_summary_limit_bytes"),
        ("CLUSTERHOP_CLEANUP_TIMEOUT_SECONDS", "0", "cleanup_timeout_seconds"),
        ("CLUSTERHOP_CLEANUP_TIMEOUT_SECONDS", "-1", "cleanup_timeout_seconds"),
    ],
)
def test_rejects_non_positive_runtime_overrides(
    environment_name: str,
    value: str,
    field_name: str,
) -> None:
    environ = valid_environment()
    environ[environment_name] = value

    with pytest.raises(ConfigurationError, match=field_name):
        load_config_from_environment(environ)


def test_rejects_pipeline_timeout_shorter_than_poll_interval() -> None:
    environ = valid_environment()
    environ["CLUSTERHOP_POLL_INTERVAL_SECONDS"] = "10"
    environ["CLUSTERHOP_PIPELINE_TIMEOUT_SECONDS"] = "5"

    with pytest.raises(ConfigurationError) as captured_error:
        load_config_from_environment(environ)

    message = str(captured_error.value)
    assert "pipeline_timeout_seconds" in message
    assert "poll_interval_seconds" in message


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_rejects_non_finite_cleanup_timeout(value: str) -> None:
    environ = valid_environment()
    environ["CLUSTERHOP_CLEANUP_TIMEOUT_SECONDS"] = value

    with pytest.raises(ConfigurationError, match="cleanup_timeout_seconds"):
        load_config_from_environment(environ)


def test_rejects_blank_pipeline_state_file() -> None:
    environ = valid_environment()
    environ["CLUSTERHOP_PIPELINE_STATE_FILE"] = "   "

    with pytest.raises(ConfigurationError, match="CLUSTERHOP_PIPELINE_STATE_FILE"):
        load_config_from_environment(environ)


@pytest.mark.parametrize("secret", [GITLAB_TOKEN, GITLAB_TRIGGER_TOKEN])
def test_rejects_remote_url_containing_secret_without_exposing_it(secret: str) -> None:
    environ = valid_environment()
    environ["GITLAB_REMOTE_URL"] = f"https://gitlab.example.test/{secret}/project.git"

    with pytest.raises(ConfigurationError) as captured_error:
        load_config_from_environment(environ)

    assert secret not in str(captured_error.value)
    assert "gitlab_remote_url" in str(captured_error.value)


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_rejects_http_remote_url_with_embedded_credentials(scheme: str) -> None:
    environ = valid_environment()
    environ["GITLAB_REMOTE_URL"] = f"{scheme}://user:password@gitlab.example.test/repo.git"

    with pytest.raises(ConfigurationError, match="embedded credentials"):
        load_config_from_environment(environ)


def test_allows_ssh_remote_url() -> None:
    environ = valid_environment()
    environ["GITLAB_REMOTE_URL"] = "git@gitlab.example.test:group/project.git"

    config = load_config_from_environment(environ)

    assert config.gitlab_remote_url == "git@gitlab.example.test:group/project.git"


def test_repr_hides_secrets_and_extra_variables() -> None:
    extra_secret = "dummy-extra-variable-secret"
    config = valid_config(extra_pipeline_variables={"SECRET": extra_secret})

    representation = repr(config)

    assert GITLAB_TOKEN not in representation
    assert GITLAB_TRIGGER_TOKEN not in representation
    assert extra_secret not in representation


def test_extra_pipeline_variables_default_is_empty_and_immutable() -> None:
    first_config = valid_config()
    second_config = valid_config()

    assert first_config.extra_pipeline_variables == {}
    assert first_config.extra_pipeline_variables is not second_config.extra_pipeline_variables
    with pytest.raises(TypeError):
        first_config.extra_pipeline_variables["KEY"] = "value"  # type: ignore[index]
