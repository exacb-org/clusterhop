# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Data models used by the GitHub-to-GitLab bridge."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GitLabPipeline:
    """Selected fields returned by GitLab pipeline API endpoints."""

    id: int
    status: str
    web_url: str | None
    ref: str | None
    sha: str | None


@dataclass(frozen=True)
class GitLabJob:
    """Selected fields returned for a job in a GitLab pipeline."""

    id: int
    name: str
    status: str
    stage: str
    duration: float | None
    web_url: str | None
