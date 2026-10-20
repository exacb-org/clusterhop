# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Configuration loading and validation for the GitHub-to-GitLab bridge."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_PIPELINE_TIMEOUT_SECONDS = 150.0
DEFAULT_LOG_SUMMARY_LIMIT_BYTES = 800_000
DEFAULT_CLEANUP_TIMEOUT_SECONDS = 10.0
GITLAB_VARIABLES_ENVIRONMENT_NAME = "CLUSTERHOP_GITLAB_VARIABLES"
_PIPELINE_VARIABLE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_PIPELINE_VARIABLE_NAMES = frozenset(
    {
        "GITHUB_COMMIT_SHA",
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ID",
    }
)


class ConfigurationError(ValueError):
    """Raised when GitHub-to-GitLab configuration is missing or invalid."""


@dataclass(frozen=True)
class GitHub2LabConfig:
    """Validated configuration for a GitHub-to-GitLab bridge run."""

    gitlab_token: str = field(repr=False)
    gitlab_trigger_token: str = field(repr=False)
    gitlab_url: str
    gitlab_remote_url: str
    gitlab_project_id: str | None
    gitlab_project_path: str | None
    github_commit_sha: str
    github_repository: str
    github_run_id: str
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    pipeline_timeout_seconds: float = DEFAULT_PIPELINE_TIMEOUT_SECONDS
    log_summary_limit_bytes: int = DEFAULT_LOG_SUMMARY_LIMIT_BYTES
    cleanup_timeout_seconds: float = DEFAULT_CLEANUP_TIMEOUT_SECONDS
    pipeline_state_file: Path | None = None
    extra_pipeline_variables: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Validate fields and protect the pipeline-variable mapping from mutation."""

        required_strings = {
            "gitlab_token": self.gitlab_token,
            "gitlab_trigger_token": self.gitlab_trigger_token,
            "gitlab_url": self.gitlab_url,
            "gitlab_remote_url": self.gitlab_remote_url,
            "github_commit_sha": self.github_commit_sha,
            "github_repository": self.github_repository,
            "github_run_id": self.github_run_id,
        }
        for field_name, value in required_strings.items():
            _validate_required_string(field_name, value)

        project_id_is_set = _is_non_empty_string(self.gitlab_project_id)
        project_path_is_set = _is_non_empty_string(self.gitlab_project_path)
        if project_id_is_set == project_path_is_set:
            raise ConfigurationError(
                "Exactly one of gitlab_project_id or gitlab_project_path must be provided."
            )

        _validate_positive_float("poll_interval_seconds", self.poll_interval_seconds)
        _validate_positive_float("pipeline_timeout_seconds", self.pipeline_timeout_seconds)
        _validate_positive_integer("log_summary_limit_bytes", self.log_summary_limit_bytes)
        _validate_positive_float("cleanup_timeout_seconds", self.cleanup_timeout_seconds)
        if self.pipeline_timeout_seconds < self.poll_interval_seconds:
            raise ConfigurationError(
                "pipeline_timeout_seconds must be greater than or equal to "
                "poll_interval_seconds."
            )
        _validate_token_free_remote_url(
            self.gitlab_remote_url,
            secrets=(self.gitlab_token, self.gitlab_trigger_token),
        )

        variables = validate_extra_pipeline_variables(self.extra_pipeline_variables)
        object.__setattr__(self, "extra_pipeline_variables", MappingProxyType(variables))


def load_config_from_environment(
    environ: Mapping[str, str] | None = None,
) -> GitHub2LabConfig:
    """Load and validate configuration from an environment-style mapping."""

    source = os.environ if environ is None else environ

    return GitHub2LabConfig(
        gitlab_token=_required_environment_value(source, "GITLAB_TOKEN"),
        gitlab_trigger_token=_required_environment_value(source, "GITLAB_TRIGGER_TOKEN"),
        gitlab_url=_required_environment_value(source, "GITLAB_URL"),
        gitlab_remote_url=_required_environment_value(source, "GITLAB_REMOTE_URL"),
        gitlab_project_id=_optional_environment_value(source, "GITLAB_PROJECT_ID"),
        gitlab_project_path=_optional_environment_value(source, "GITLAB_PROJECT_PATH"),
        github_commit_sha=_required_environment_value(source, "GITHUB_SHA"),
        github_repository=_required_environment_value(source, "GITHUB_REPOSITORY"),
        github_run_id=_required_environment_value(source, "GITHUB_RUN_ID"),
        poll_interval_seconds=_float_environment_value(
            source,
            "CLUSTERHOP_POLL_INTERVAL_SECONDS",
            DEFAULT_POLL_INTERVAL_SECONDS,
        ),
        pipeline_timeout_seconds=_float_environment_value(
            source,
            "CLUSTERHOP_PIPELINE_TIMEOUT_SECONDS",
            DEFAULT_PIPELINE_TIMEOUT_SECONDS,
        ),
        log_summary_limit_bytes=_integer_environment_value(
            source,
            "CLUSTERHOP_LOG_SUMMARY_LIMIT_BYTES",
            DEFAULT_LOG_SUMMARY_LIMIT_BYTES,
        ),
        cleanup_timeout_seconds=_float_environment_value(
            source,
            "CLUSTERHOP_CLEANUP_TIMEOUT_SECONDS",
            DEFAULT_CLEANUP_TIMEOUT_SECONDS,
        ),
        pipeline_state_file=_path_environment_value(
            source,
            "CLUSTERHOP_PIPELINE_STATE_FILE",
        ),
        extra_pipeline_variables=_pipeline_variables_environment_value(source),
    )


def validate_extra_pipeline_variables(value: object) -> dict[str, str]:
    """Return a validated copy of user-supplied GitLab trigger variables."""

    if not isinstance(value, Mapping):
        raise ConfigurationError(
            "extra_pipeline_variables must be a mapping of string names to string values."
        )

    variables: dict[str, str] = {}
    for name, variable_value in value.items():
        if not isinstance(name, str) or not isinstance(variable_value, str):
            raise ConfigurationError(
                "extra_pipeline_variables must be a mapping of string names to string values."
            )
        if _PIPELINE_VARIABLE_NAME_PATTERN.fullmatch(name) is None:
            raise ConfigurationError("extra_pipeline_variables contains an invalid variable name.")
        if name in _RESERVED_PIPELINE_VARIABLE_NAMES:
            raise ConfigurationError(
                "extra_pipeline_variables must not override GitHub metadata variables."
            )
        variables[name] = variable_value
    return variables


def _is_non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_required_string(field_name: str, value: object) -> None:
    if not _is_non_empty_string(value):
        raise ConfigurationError(f"Required configuration field {field_name} is missing or empty.")


def _validate_positive_float(field_name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigurationError(f"Configuration field {field_name} must be greater than 0.")


def _validate_positive_integer(field_name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"Configuration field {field_name} must be greater than 0.")


def _validate_token_free_remote_url(remote_url: str, secrets: tuple[str, ...]) -> None:
    if any(secret and secret in remote_url for secret in secrets):
        raise ConfigurationError("gitlab_remote_url must not contain authentication secrets.")

    try:
        parsed_url = urlsplit(remote_url)
        has_http_credentials = parsed_url.scheme.lower() in {"http", "https"} and (
            parsed_url.username is not None or parsed_url.password is not None
        )
    except ValueError as error:
        raise ConfigurationError("gitlab_remote_url is invalid.") from error

    if has_http_credentials:
        raise ConfigurationError("gitlab_remote_url must not contain embedded credentials.")


def _required_environment_value(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not _is_non_empty_string(value):
        raise ConfigurationError(f"Required environment variable {name} is missing or empty.")
    assert isinstance(value, str)
    return value


def _optional_environment_value(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    return value if _is_non_empty_string(value) else None


def _float_environment_value(
    environ: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    value = environ.get(name)
    if value is None:
        return default

    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"Environment variable {name} must be numeric.") from error


def _integer_environment_value(
    environ: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    value = environ.get(name)
    if value is None:
        return default

    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"Environment variable {name} must be an integer.") from error


def _path_environment_value(environ: Mapping[str, str], name: str) -> Path | None:
    value = environ.get(name)
    if value is None:
        return None
    if not value.strip():
        raise ConfigurationError(f"Environment variable {name} must not be empty.")
    return Path(value)


def _pipeline_variables_environment_value(environ: Mapping[str, str]) -> dict[str, str]:
    encoded = environ.get(GITLAB_VARIABLES_ENVIRONMENT_NAME)
    if encoded is None:
        return {}

    try:
        loaded: object = json.loads(encoded)
    except (json.JSONDecodeError, TypeError):
        raise ConfigurationError(
            f"Environment variable {GITLAB_VARIABLES_ENVIRONMENT_NAME} must contain a valid "
            "JSON object."
        ) from None

    try:
        return validate_extra_pipeline_variables(loaded)
    except ConfigurationError:
        raise ConfigurationError(
            f"Environment variable {GITLAB_VARIABLES_ENVIRONMENT_NAME} must contain a valid "
            "mapping of pipeline variable names to string values."
        ) from None
