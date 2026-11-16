# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Authenticated GitLab project metadata resolution."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from urllib.parse import quote

from clusterhop.github2lab.gitlab import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    HttpTransport,
    HttpTransportError,
    UrllibHttpTransport,
)
from clusterhop.github2lab.urls import (
    GitLabUrlError,
    validate_gitlab_base_url,
    validate_resolved_https_git_remote,
)


class GitLabProjectResolutionError(RuntimeError):
    """A sanitized GitLab project-resolution failure."""


@dataclass(frozen=True)
class ResolvedGitLabProject:
    """Authoritative project fields required by the bridge."""

    id: int
    path_with_namespace: str
    http_url_to_repo: str


def resolve_gitlab_project(
    *,
    gitlab_url: str,
    project_identifier: str,
    gitlab_token: str,
    transport: HttpTransport | None = None,
    request_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> ResolvedGitLabProject:
    """Resolve a numeric project ID and HTTP Git URL through the GitLab Projects API."""

    try:
        normalized_gitlab_url = validate_gitlab_base_url(gitlab_url)
    except GitLabUrlError:
        raise GitLabProjectResolutionError("GitLab project resolution configuration is invalid.")
    if not isinstance(project_identifier, str) or not project_identifier.strip():
        raise GitLabProjectResolutionError("GitLab project resolution configuration is invalid.")
    if not isinstance(gitlab_token, str) or not gitlab_token.strip():
        raise GitLabProjectResolutionError("GitLab project resolution configuration is invalid.")
    if (
        isinstance(request_timeout_seconds, bool)
        or not isinstance(request_timeout_seconds, (int, float))
        or not math.isfinite(request_timeout_seconds)
        or request_timeout_seconds <= 0
    ):
        raise GitLabProjectResolutionError("GitLab project resolution timeout is invalid.")

    encoded_project = quote(project_identifier.strip(), safe="")
    url = f"{normalized_gitlab_url}/api/v4/projects/{encoded_project}"
    http = transport if transport is not None else UrllibHttpTransport()
    try:
        response = http.request(
            method="GET",
            url=url,
            headers={"Accept": "application/json", "PRIVATE-TOKEN": gitlab_token},
            body=None,
            timeout=float(request_timeout_seconds),
        )
    except HttpTransportError:
        raise GitLabProjectResolutionError("Unable to resolve the GitLab project.") from None

    if response.status_code != 200:
        raise GitLabProjectResolutionError("Unable to resolve the GitLab project.")

    try:
        payload: object = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GitLabProjectResolutionError("GitLab returned invalid project metadata.") from None
    if not isinstance(payload, dict):
        raise GitLabProjectResolutionError("GitLab returned invalid project metadata.")

    project_id = payload.get("id")
    project_path = payload.get("path_with_namespace")
    remote_url_value = payload.get("http_url_to_repo")
    try:
        remote_url = validate_resolved_https_git_remote(remote_url_value, normalized_gitlab_url)
    except GitLabUrlError:
        raise GitLabProjectResolutionError("GitLab returned invalid project metadata.") from None
    if (
        isinstance(project_id, bool)
        or not isinstance(project_id, int)
        or project_id <= 0
        or not isinstance(project_path, str)
        or not project_path.strip()
    ):
        raise GitLabProjectResolutionError("GitLab returned invalid project metadata.")

    return ResolvedGitLabProject(
        id=project_id,
        path_with_namespace=project_path,
        http_url_to_repo=remote_url,
    )
