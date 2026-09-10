# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Public Python API for ClusterHop runs."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from clusterhop.github2lab.config import ConfigurationError
from clusterhop.github2lab.runner import run_from_environment

RunStatus = Literal["success", "failed", "canceled", "skipped", "timeout", "error"]
InputObject = Mapping[str, object] | object

_MISSING = object()
_CONFIG_ENVIRONMENT_FIELDS = {
    "gitlab_url": "GITLAB_URL",
    "gitlab_remote_url": "GITLAB_REMOTE_URL",
    "gitlab_project_id": "GITLAB_PROJECT_ID",
    "gitlab_project_path": "GITLAB_PROJECT_PATH",
    "github_commit_sha": "GITHUB_SHA",
    "github_repository": "GITHUB_REPOSITORY",
    "github_run_id": "GITHUB_RUN_ID",
}
_OPTION_ENVIRONMENT_FIELDS = {
    "poll_interval_seconds": "CLUSTERHOP_POLL_INTERVAL_SECONDS",
    "pipeline_timeout_seconds": "CLUSTERHOP_PIPELINE_TIMEOUT_SECONDS",
    "pipeline_state_file": "CLUSTERHOP_PIPELINE_STATE_FILE",
    "cleanup_timeout_seconds": "CLUSTERHOP_CLEANUP_TIMEOUT_SECONDS",
}


class RunConfigurationError(ValueError):
    """Raised when public run inputs are missing or invalid."""


class UnsupportedDestinationError(RunConfigurationError):
    """Raised when no implementation exists for the requested destination type."""


@dataclass(frozen=True)
class RunResult:
    """Stable, destination-neutral result returned by :func:`clusterhop.run`."""

    status: RunStatus
    exit_code: int
    summary: str = field(repr=False)
    error_message: str | None = field(default=None, repr=False)


def run(
    *,
    config: InputObject,
    repo_path: str | Path,
    options: InputObject | None = None,
    secrets: InputObject | None = None,
) -> RunResult:
    """Run ClusterHop against an already-loaded destination configuration.

    This initial public adapter supports only the existing GitLab destination. Configuration,
    options, and secrets are Python mappings or objects with matching attributes; loading files
    and decrypting secrets intentionally remain outside this function.
    """

    destination_type = _destination_type(config)
    if destination_type.casefold() != "gitlab":
        raise UnsupportedDestinationError(
            "The requested destination type is not supported by this ClusterHop version."
        )

    environment = _github_metadata_from_environment()
    _copy_config_fields(config, environment)
    _copy_secret_fields(secrets, environment)
    _copy_option_fields(options, environment)

    try:
        internal_result = run_from_environment(
            environment,
            repository_path=repo_path,
        )
    except ConfigurationError as error:
        raise RunConfigurationError(str(error)) from None

    return RunResult(
        status=internal_result.status,
        exit_code=internal_result.exit_code,
        summary=internal_result.summary,
        error_message=internal_result.error_message,
    )


def _destination_type(config: InputObject) -> str:
    destination_type = _input_value(config, "destination_type")
    legacy_type = _input_value(config, "type")
    if destination_type is not _MISSING and legacy_type is not _MISSING:
        raise RunConfigurationError(
            "Specify only one of destination_type or type in the run configuration."
        )
    value = destination_type if destination_type is not _MISSING else legacy_type
    if not isinstance(value, str) or not value.strip():
        raise RunConfigurationError(
            "Required run configuration field destination_type is missing or empty."
        )
    return value.strip()


def _github_metadata_from_environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    for name in (
        "GITHUB_SHA",
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ID",
        "GITHUB_STEP_SUMMARY",
        "CLUSTERHOP_LOG_SUMMARY_LIMIT_BYTES",
    ):
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value
    return environment


def _copy_config_fields(source: InputObject, environment: dict[str, str]) -> None:
    for field_name, environment_name in _CONFIG_ENVIRONMENT_FIELDS.items():
        value = _input_value(source, field_name)
        if value is _MISSING or value is None:
            continue
        if (
            field_name == "gitlab_project_id"
            and isinstance(value, int)
            and not isinstance(value, bool)
        ):
            environment[environment_name] = str(value)
            continue
        if not isinstance(value, str):
            raise RunConfigurationError(f"Run configuration field {field_name} must be a string.")
        environment[environment_name] = value


def _copy_secret_fields(secrets: InputObject | None, environment: dict[str, str]) -> None:
    if secrets is None:
        raise RunConfigurationError("Required run secrets are missing.")
    for field_name, environment_name in (
        ("gitlab_token", "GITLAB_TOKEN"),
        ("gitlab_trigger_token", "GITLAB_TRIGGER_TOKEN"),
    ):
        value = _input_value(secrets, field_name)
        if not isinstance(value, str) or not value.strip():
            raise RunConfigurationError(
                f"Required run secret field {field_name} is missing or empty."
            )
        environment[environment_name] = value


def _copy_option_fields(options: InputObject | None, environment: dict[str, str]) -> None:
    if options is None:
        return
    for field_name, environment_name in _OPTION_ENVIRONMENT_FIELDS.items():
        value = _input_value(options, field_name)
        if value is _MISSING or value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Path)):
            raise RunConfigurationError(f"Run option {field_name} has an invalid value.")
        environment[environment_name] = str(value)


def _input_value(source: InputObject, name: str) -> object:
    if isinstance(source, Mapping):
        return source.get(name, _MISSING)
    try:
        return getattr(source, name, _MISSING)
    except Exception:
        raise RunConfigurationError("Unable to read the supplied run input object.") from None
