# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Workflow-level fallback cancellation for an interrupted github2lab runner."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from clusterhop.github2lab.config import GitHub2LabConfig, load_config_from_environment
from clusterhop.github2lab.gitlab import GitLabClient
from clusterhop.github2lab.models import GitLabPipeline
from clusterhop.github2lab.state import (
    FilePipelineStateStore,
    PipelineState,
    PipelineStateError,
    PipelineStateStorage,
    is_terminal_pipeline_status,
    project_identifier_from_config,
)

FallbackAction = Literal["no_state", "already_inactive", "already_terminal", "canceled"]


class FallbackCancellationError(RuntimeError):
    """A sanitized fallback cancellation failure."""


class FallbackGitLabOperations(Protocol):
    """GitLab operations required by fallback cancellation."""

    def get_pipeline(self, pipeline_id: int) -> GitLabPipeline:
        """Retrieve current pipeline state."""

    def cancel_pipeline(
        self,
        pipeline_id: int,
        *,
        timeout_seconds: float | None = None,
    ) -> GitLabPipeline:
        """Cancel an active pipeline with a bounded request."""


@dataclass(frozen=True)
class FallbackCancellationResult:
    """Secret-free outcome of a fallback cancellation attempt."""

    action: FallbackAction
    pipeline_id: int | None


def cancel_pipeline_from_state(
    config: GitHub2LabConfig,
    state_store: PipelineStateStorage,
    gitlab: FallbackGitLabOperations,
) -> FallbackCancellationResult:
    """Cancel a matching active pipeline and atomically update its persisted state."""

    try:
        state = state_store.read()
        if state is None:
            return FallbackCancellationResult("no_state", None)
        _validate_state_matches_config(state, config)
        if not state.active:
            return FallbackCancellationResult("already_inactive", state.pipeline_id)

        pipeline = gitlab.get_pipeline(state.pipeline_id)
        if pipeline.id != state.pipeline_id:
            raise FallbackCancellationError(
                "Fallback cancellation received an unexpected pipeline identifier."
            )
        if is_terminal_pipeline_status(pipeline.status):
            state_store.write(state.with_pipeline(pipeline, active=False))
            return FallbackCancellationResult("already_terminal", state.pipeline_id)

        canceled_pipeline = gitlab.cancel_pipeline(
            state.pipeline_id,
            timeout_seconds=config.cleanup_timeout_seconds,
        )
        if canceled_pipeline.id != state.pipeline_id or not is_terminal_pipeline_status(
            canceled_pipeline.status
        ):
            raise FallbackCancellationError(
                "GitLab did not confirm fallback pipeline cancellation."
            )
        state_store.write(state.with_pipeline(canceled_pipeline, active=False))
        return FallbackCancellationResult("canceled", state.pipeline_id)
    except FallbackCancellationError:
        raise
    except Exception:
        raise FallbackCancellationError("Fallback pipeline cancellation failed.") from None


def cancel_pipeline_from_state_environment(
    environ: Mapping[str, str] | None = None,
) -> FallbackCancellationResult:
    """Load workflow state and configuration, then perform fallback cancellation."""

    source = os.environ if environ is None else environ
    state_path_value = source.get("CLUSTERHOP_PIPELINE_STATE_FILE")
    if state_path_value is None:
        return FallbackCancellationResult("no_state", None)
    if not state_path_value.strip():
        raise FallbackCancellationError("Fallback pipeline state path is invalid.")

    state_store = FilePipelineStateStore(Path(state_path_value))
    try:
        state = state_store.read()
    except PipelineStateError:
        raise FallbackCancellationError("Fallback pipeline state is invalid.") from None
    if state is None:
        return FallbackCancellationResult("no_state", None)

    try:
        config = load_config_from_environment(source)
        gitlab = GitLabClient(
            config,
            request_timeout_seconds=config.cleanup_timeout_seconds,
        )
    except Exception:
        raise FallbackCancellationError("Fallback cancellation configuration is invalid.") from None
    return cancel_pipeline_from_state(config, state_store, gitlab)


def main() -> int:
    """Run fallback cancellation as ``python -m clusterhop.github2lab.fallback``."""

    try:
        result = cancel_pipeline_from_state_environment()
    except FallbackCancellationError as error:
        print(str(error), file=sys.stderr)
        return 1

    messages: Mapping[FallbackAction, str] = {
        "no_state": "No active GitLab pipeline state was recorded.",
        "already_inactive": "The recorded GitLab pipeline is already inactive.",
        "already_terminal": "The recorded GitLab pipeline is already terminal.",
        "canceled": "The active GitLab pipeline was canceled by fallback cleanup.",
    }
    print(messages[result.action])
    return 0


def _validate_state_matches_config(state: PipelineState, config: GitHub2LabConfig) -> None:
    if (
        state.github_run_id != config.github_run_id
        or state.project_identifier != project_identifier_from_config(config)
    ):
        raise FallbackCancellationError(
            "Fallback pipeline state does not match the current workflow configuration."
        )


if __name__ == "__main__":
    raise SystemExit(main())
