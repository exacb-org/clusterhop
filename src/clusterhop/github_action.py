# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Thin lifecycle adapter for the reusable ClusterHop GitHub Action."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from clusterhop.api import RunConfigurationError, _prepare_gitlab_environment
from clusterhop.cli import (
    CliInputError,
    _load_secrets_mapping,
    _load_secrets_yaml_mapping,
    _load_yaml_mapping,
)
from clusterhop.github2lab.fallback import (
    FallbackCancellationError,
    cancel_pipeline_from_state_environment,
)
from clusterhop.github2lab.config import DEFAULT_CLEANUP_TIMEOUT_SECONDS
from clusterhop.github2lab.machine import MachineNameError, jsc_machine_pipeline_variable
from clusterhop.github2lab.state import FilePipelineStateStore, PipelineStateError

_ACTION_SCHEMA_VERSION = 1
_ACTION_DIRECTORY_NAME = ".clusterhop-action"
_KEYLOOM_MARKER = "__keyloom__"
_KEYLOOM_SECRET_TYPE = "secret"
_KEYLOOM_ENVELOPE_PREFIX = "$KEYLOOM;"
_REQUIRED_ENCRYPTED_SECRET_FIELDS = ("gitlab_token", "gitlab_trigger_token")


class GitHubActionError(RuntimeError):
    """A sanitized reusable-Action setup or cleanup failure."""


@dataclass(frozen=True)
class ActionPaths:
    """Files shared by the Docker main and post containers."""

    directory: Path
    config: Path
    options: Path
    pipeline_state: Path
    metadata: Path


def prepare_action_run(
    *,
    gitlab_url: str,
    gitlab_project: str,
    jsc_machine: str,
    secrets_file: str,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Validate Action inputs, create non-secret inputs, and return the CLI command."""

    source = os.environ if environ is None else environ
    workspace = _required_directory(source, "GITHUB_WORKSPACE")
    paths = action_paths(source)
    normalized_gitlab_url = _validate_gitlab_url(gitlab_url)
    normalized_project = _validate_gitlab_project(gitlab_project)
    normalized_machine = jsc_machine
    if normalized_machine:
        try:
            jsc_machine_pipeline_variable(normalized_machine)
        except MachineNameError as error:
            raise GitHubActionError(str(error)) from None

    secrets_path = _resolve_secrets_path(workspace, secrets_file)
    try:
        unresolved_secrets = _load_secrets_yaml_mapping(secrets_path)
    except CliInputError:
        raise GitHubActionError("Unable to validate the Keyloom secrets file.") from None
    if not _has_required_keyloom_secrets(unresolved_secrets):
        raise GitHubActionError("The reusable Action requires a Keyloom-encrypted secrets file.")
    try:
        paths.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(paths.directory, 0o700)
    except OSError:
        raise GitHubActionError("Unable to prepare the ClusterHop Action workspace.") from None

    config: dict[str, object] = {
        "destination_type": "gitlab",
        "gitlab_url": normalized_gitlab_url,
        "gitlab_project_path": normalized_project,
    }
    if normalized_machine:
        config["jsc_machine"] = normalized_machine
    options = {"pipeline_state_file": str(paths.pipeline_state)}
    metadata = {
        "schema_version": _ACTION_SCHEMA_VERSION,
        "config_file": str(paths.config),
        "options_file": str(paths.options),
        "secrets_file": str(secrets_path),
        "pipeline_state_file": str(paths.pipeline_state),
    }
    _atomic_write_json(paths.config, config)
    _atomic_write_json(paths.options, options)
    _atomic_write_json(paths.metadata, metadata)

    return (
        "clusterhop",
        "run",
        "--config",
        str(paths.config),
        "--options",
        str(paths.options),
        str(workspace),
        "--secrets",
        str(secrets_path),
    )


def cleanup_action_run(environ: Mapping[str, str] | None = None) -> str:
    """Run idempotent fallback cancellation when persisted state is still active."""

    source = os.environ if environ is None else environ
    paths = action_paths(source)
    workspace = _required_directory(source, "GITHUB_WORKSPACE")
    if not paths.metadata.is_file():
        _remove_action_directory(paths.directory)
        return "no_state"

    metadata = _load_action_metadata(paths.metadata)
    if (
        Path(metadata["config_file"]) != paths.config
        or Path(metadata["options_file"]) != paths.options
    ):
        raise GitHubActionError("ClusterHop Action state metadata is invalid.")
    state_path = Path(metadata["pipeline_state_file"])
    if state_path != paths.pipeline_state:
        raise GitHubActionError("ClusterHop Action state metadata is invalid.")
    try:
        state = FilePipelineStateStore(state_path).read()
    except PipelineStateError:
        raise GitHubActionError("ClusterHop Action pipeline state is invalid.") from None
    if state is None:
        _remove_action_directory(paths.directory)
        return "no_state"
    if not state.active:
        _remove_action_directory(paths.directory)
        return "already_inactive"

    secrets_path = _validated_metadata_secrets_path(workspace, metadata["secrets_file"])
    try:
        config = _load_yaml_mapping(Path(metadata["config_file"]), "configuration")
        options = _load_yaml_mapping(Path(metadata["options_file"]), "options")
        secrets = _load_secrets_mapping(secrets_path, source)
        environment = _prepare_gitlab_environment(
            config,
            options,
            secrets,
            project_resolution_timeout_seconds=DEFAULT_CLEANUP_TIMEOUT_SECONDS,
        )
        result = cancel_pipeline_from_state_environment(environment)
    except (CliInputError, RunConfigurationError, FallbackCancellationError):
        raise GitHubActionError("ClusterHop Action fallback cleanup failed.") from None
    except Exception:
        raise GitHubActionError("ClusterHop Action fallback cleanup failed.") from None

    _remove_action_directory(paths.directory)
    return result.action


def action_paths(environ: Mapping[str, str] | None = None) -> ActionPaths:
    """Return deterministic, shared paths for one Action invocation."""

    source = os.environ if environ is None else environ
    home = _required_directory(source, "HOME")
    identity_parts = tuple(
        source.get(name, "")
        for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB", "GITHUB_ACTION")
    )
    if any(not value.strip() for value in identity_parts):
        raise GitHubActionError("Required GitHub Actions runtime metadata is unavailable.")
    digest = hashlib.sha256("\0".join(identity_parts).encode("utf-8")).hexdigest()[:24]
    directory = home / _ACTION_DIRECTORY_NAME / digest
    return ActionPaths(
        directory=directory,
        config=directory / "config.yaml",
        options=directory / "options.yaml",
        pipeline_state=directory / "pipeline-state.json",
        metadata=directory / "action-state.json",
    )


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the Docker Action main or post-cleanup operation."""

    argv = tuple(sys.argv[1:] if arguments is None else arguments)
    try:
        if not argv:
            raise GitHubActionError("ClusterHop Action operation is missing.")
        if argv[0] == "main":
            if len(argv) != 5:
                raise GitHubActionError("ClusterHop Action inputs are invalid.")
            command = prepare_action_run(
                gitlab_url=argv[1],
                gitlab_project=argv[2],
                jsc_machine=argv[3],
                secrets_file=argv[4],
            )
            try:
                os.execvp(command[0], command)
            except OSError:
                raise GitHubActionError("Unable to launch the ClusterHop CLI.") from None
            raise AssertionError("os.execvp returned unexpectedly.")
        if argv == ("cleanup",):
            action = cleanup_action_run()
            print(f"ClusterHop Action cleanup: {action}.")
            return 0
        raise GitHubActionError("ClusterHop Action operation is invalid.")
    except GitHubActionError as error:
        print(f"ClusterHop Action error: {error}", file=sys.stderr)
        return 2


def _required_directory(environ: Mapping[str, str], name: str) -> Path:
    value = environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise GitHubActionError("Required GitHub Actions runtime paths are unavailable.")
    path = Path(value).expanduser()
    try:
        if not path.is_dir():
            raise GitHubActionError("Required GitHub Actions runtime paths are unavailable.")
        return path.resolve()
    except OSError:
        raise GitHubActionError("Required GitHub Actions runtime paths are unavailable.") from None


def _validate_gitlab_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GitHubActionError("Action input gitlab-url is required.")
    normalized = value.strip().rstrip("/")
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        raise GitHubActionError("Action input gitlab-url is invalid.") from None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\n" in normalized
        or "\r" in normalized
    ):
        raise GitHubActionError("Action input gitlab-url is invalid.")
    return normalized


def _validate_gitlab_project(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GitHubActionError("Action input gitlab-project is required.")
    normalized = value.strip()
    segments = normalized.split("/")
    if (
        len(normalized) > 1024
        or normalized.startswith("/")
        or normalized.endswith("/")
        or any(not segment or segment in {".", ".."} for segment in segments)
        or any(character.isspace() or ord(character) < 32 for character in normalized)
    ):
        raise GitHubActionError("Action input gitlab-project is invalid.")
    return normalized


def _resolve_secrets_path(workspace: Path, value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise GitHubActionError("Action input secrets-file is required.")
    relative_path = Path(value.strip())
    if relative_path.is_absolute():
        raise GitHubActionError("Action input secrets-file must be a relative repository path.")
    try:
        path = (workspace / relative_path).resolve(strict=True)
        path.relative_to(workspace)
    except (OSError, ValueError):
        raise GitHubActionError(
            "Action input secrets-file must identify a file inside the repository."
        ) from None
    if not path.is_file():
        raise GitHubActionError(
            "Action input secrets-file must identify a file inside the repository."
        )
    return path


def _has_required_keyloom_secrets(secrets: Mapping[str, object]) -> bool:
    """Enforce both wrappers without attempting cryptographic validation.

    Native Keyloom loading in the CLI remains responsible for parsing and authenticating the
    ciphertext. This check only enforces the Action's encrypted-credential input contract before
    the CLI starts.
    """

    for field in _REQUIRED_ENCRYPTED_SECRET_FIELDS:
        wrapper = secrets.get(field)
        if not isinstance(wrapper, Mapping) or set(wrapper) != {_KEYLOOM_MARKER, "value"}:
            return False
        if wrapper.get(_KEYLOOM_MARKER) != _KEYLOOM_SECRET_TYPE:
            return False
        encrypted_value = wrapper.get("value")
        if not isinstance(encrypted_value, str) or not encrypted_value.startswith(
            _KEYLOOM_ENVELOPE_PREFIX
        ):
            return False
    return True


def _validated_metadata_secrets_path(workspace: Path, value: str) -> Path:
    try:
        path = Path(value).resolve(strict=True)
        path.relative_to(workspace)
    except (OSError, ValueError):
        raise GitHubActionError("ClusterHop Action state metadata is invalid.") from None
    if not path.is_file():
        raise GitHubActionError("ClusterHop Action state metadata is invalid.")
    return path


def _atomic_write_json(path: Path, value: object) -> None:
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        os.chmod(path, 0o600)
    except (OSError, TypeError, ValueError):
        raise GitHubActionError("Unable to prepare the ClusterHop Action workspace.") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _load_action_metadata(path: Path) -> dict[str, str]:
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise GitHubActionError("ClusterHop Action state metadata is invalid.") from None
    expected = {
        "schema_version",
        "config_file",
        "options_file",
        "secrets_file",
        "pipeline_state_file",
    }
    if not isinstance(loaded, dict) or set(loaded) != expected:
        raise GitHubActionError("ClusterHop Action state metadata is invalid.")
    if loaded.get("schema_version") != _ACTION_SCHEMA_VERSION:
        raise GitHubActionError("ClusterHop Action state metadata is invalid.")
    result: dict[str, str] = {}
    for name in expected - {"schema_version"}:
        value = loaded.get(name)
        if not isinstance(value, str) or not value:
            raise GitHubActionError("ClusterHop Action state metadata is invalid.")
        result[name] = value
    return result


def _remove_action_directory(path: Path) -> None:
    try:
        if path.exists():
            shutil.rmtree(path)
    except OSError:
        raise GitHubActionError("Unable to remove ClusterHop Action temporary files.") from None


if __name__ == "__main__":
    raise SystemExit(main())
