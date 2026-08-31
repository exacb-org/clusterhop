# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Persistent, secret-free lifecycle state for active GitLab pipelines."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, cast

from clusterhop.github2lab.config import GitHub2LabConfig
from clusterhop.github2lab.models import GitLabPipeline

PIPELINE_STATE_SCHEMA_VERSION = 1
MAX_PIPELINE_STATE_BYTES = 64 * 1024
TERMINAL_PIPELINE_STATUSES = frozenset(
    {"success", "failed", "canceled", "cancelled", "skipped", "timeout", "timed_out"}
)
CANCELLATION_ACCEPTED_PIPELINE_STATUSES = frozenset({"canceling", "canceled", "cancelled"})
_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "active",
        "pipeline_id",
        "pipeline_status",
        "gitlab_ref",
        "project_identifier",
        "github_run_id",
    }
)


class PipelineStateError(RuntimeError):
    """A sanitized persistent-state failure."""


@dataclass(frozen=True)
class PipelineState:
    """Allowlisted, non-secret state needed for fallback cancellation."""

    active: bool
    pipeline_id: int
    pipeline_status: str
    gitlab_ref: str
    project_identifier: str
    github_run_id: str
    schema_version: int = PIPELINE_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PIPELINE_STATE_SCHEMA_VERSION:
            raise PipelineStateError("Pipeline state has an unsupported schema version.")
        if not isinstance(self.active, bool):
            raise PipelineStateError("Pipeline state field active is invalid.")
        if (
            isinstance(self.pipeline_id, bool)
            or not isinstance(self.pipeline_id, int)
            or self.pipeline_id <= 0
        ):
            raise PipelineStateError("Pipeline state field pipeline_id is invalid.")
        for name, value in (
            ("pipeline_status", self.pipeline_status),
            ("gitlab_ref", self.gitlab_ref),
            ("project_identifier", self.project_identifier),
            ("github_run_id", self.github_run_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise PipelineStateError(f"Pipeline state field {name} is invalid.")

    def with_pipeline(self, pipeline: GitLabPipeline, *, active: bool) -> PipelineState:
        """Return updated state for a newly observed pipeline response."""

        if pipeline.id != self.pipeline_id:
            raise PipelineStateError("Pipeline state has an unexpected pipeline identifier.")
        return replace(self, active=active, pipeline_status=pipeline.status)


class PipelineStateStorage(Protocol):
    """Storage operations consumed by the runner and fallback command."""

    def read(self) -> PipelineState | None:
        """Read state, or return None when no state has been recorded."""

    def write(self, state: PipelineState) -> None:
        """Atomically persist one complete state record."""


class NullPipelineStateStore:
    """No-op state store used when persistence is not configured."""

    def read(self) -> PipelineState | None:
        return None

    def write(self, state: PipelineState) -> None:
        del state


class FilePipelineStateStore:
    """Versioned JSON state store using atomic same-directory replacement."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def read(self) -> PipelineState | None:
        """Read and strictly validate a state file without exposing its contents."""

        try:
            if not self._path.exists():
                return None
            if self._path.stat().st_size > MAX_PIPELINE_STATE_BYTES:
                raise PipelineStateError("Pipeline state file is invalid.")
            with self._path.open("r", encoding="utf-8") as state_file:
                payload = json.load(state_file)
        except PipelineStateError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise PipelineStateError("Unable to read the pipeline state file.") from None

        if not isinstance(payload, dict) or frozenset(payload) != _STATE_FIELDS:
            raise PipelineStateError("Pipeline state file is invalid.")
        values = cast(dict[str, object], payload)
        try:
            return PipelineState(
                schema_version=values["schema_version"],  # type: ignore[arg-type]
                active=values["active"],  # type: ignore[arg-type]
                pipeline_id=values["pipeline_id"],  # type: ignore[arg-type]
                pipeline_status=values["pipeline_status"],  # type: ignore[arg-type]
                gitlab_ref=values["gitlab_ref"],  # type: ignore[arg-type]
                project_identifier=values["project_identifier"],  # type: ignore[arg-type]
                github_run_id=values["github_run_id"],  # type: ignore[arg-type]
            )
        except PipelineStateError:
            raise
        except (TypeError, ValueError):
            raise PipelineStateError("Pipeline state file is invalid.") from None

    def write(self, state: PipelineState) -> None:
        """Write state with mode 0600 and atomically replace the previous record."""

        payload = {
            "schema_version": state.schema_version,
            "active": state.active,
            "pipeline_id": state.pipeline_id,
            "pipeline_status": state.pipeline_status,
            "gitlab_ref": state.gitlab_ref,
            "project_identifier": state.project_identifier,
            "github_run_id": state.github_run_id,
        }
        parent = self._path.parent
        temporary_path: Path | None = None
        try:
            if not parent.is_dir():
                raise PipelineStateError("Pipeline state directory is unavailable.")
            descriptor, temporary_name = tempfile.mkstemp(
                dir=parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as state_file:
                    json.dump(payload, state_file, sort_keys=True, separators=(",", ":"))
                    state_file.write("\n")
                    state_file.flush()
                    os.fsync(state_file.fileno())
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            os.replace(temporary_path, self._path)
            temporary_path = None
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
            _fsync_directory(parent)
        except PipelineStateError:
            raise
        except (OSError, TypeError, ValueError):
            raise PipelineStateError("Unable to write the pipeline state file.") from None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass


def pipeline_state_from_config(
    config: GitHub2LabConfig,
    pipeline: GitLabPipeline,
    *,
    active: bool,
) -> PipelineState:
    """Build an allowlisted state record from validated configuration and pipeline data."""

    return PipelineState(
        active=active,
        pipeline_id=pipeline.id,
        pipeline_status=pipeline.status,
        gitlab_ref=pipeline.ref or f"ci/{config.github_commit_sha}",
        project_identifier=project_identifier_from_config(config),
        github_run_id=config.github_run_id,
    )


def project_identifier_from_config(config: GitHub2LabConfig) -> str:
    """Return a stable, unambiguous non-secret project identity."""

    if config.gitlab_project_id is not None:
        return f"id:{config.gitlab_project_id}"
    assert config.gitlab_project_path is not None
    return f"path:{config.gitlab_project_path}"


def is_terminal_pipeline_status(status: str) -> bool:
    """Return whether GitLab status represents a completed pipeline."""

    return status.strip().casefold() in TERMINAL_PIPELINE_STATUSES


def is_cancellation_accepted_status(status: str) -> bool:
    """Return whether GitLab accepted or completed a pipeline cancellation request."""

    return status.strip().casefold() in CANCELLATION_ACCEPTED_PIPELINE_STATUSES


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
