# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Authenticated GitLab project metadata resolution."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from clusterhop.github2lab.gitlab import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    HttpTransport,
    HttpTransportError,
    UrllibHttpTransport,
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

    normalized_gitlab_url = gitlab_url.strip() if isinstance(gitlab_url, str) else ""
    if (
        not isinstance(gitlab_url, str)
        or not normalized_gitlab_url
        or not _is_safe_gitlab_base_url(normalized_gitlab_url)
    ):
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
    normalized_gitlab_url = normalized_gitlab_url.rstrip("/")
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
    remote_url = payload.get("http_url_to_repo")
    if (
        isinstance(project_id, bool)
        or not isinstance(project_id, int)
        or project_id <= 0
        or not isinstance(project_path, str)
        or not project_path.strip()
        or not isinstance(remote_url, str)
        or not remote_url.strip()
        or not _is_safe_http_git_url(remote_url)
        or not _has_same_origin(normalized_gitlab_url, remote_url)
    ):
        raise GitLabProjectResolutionError("GitLab returned invalid project metadata.")

    return ResolvedGitLabProject(
        id=project_id,
        path_with_namespace=project_path,
        http_url_to_repo=remote_url,
    )


def _is_safe_http_git_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and "\n" not in value
        and "\r" not in value
    )


def _is_safe_gitlab_base_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and "\n" not in value
        and "\r" not in value
    )


def _has_same_origin(base_url: str, remote_url: str) -> bool:
    try:
        base = urlsplit(base_url)
        remote = urlsplit(remote_url)
        return (
            base.scheme.casefold() == remote.scheme.casefold()
            and base.hostname == remote.hostname
            and _effective_port(base.scheme, base.port)
            == _effective_port(remote.scheme, remote.port)
        )
    except ValueError:
        return False


def _effective_port(scheme: str, explicit_port: int | None) -> int:
    if explicit_port is not None:
        return explicit_port
    return 443 if scheme.casefold() == "https" else 80
