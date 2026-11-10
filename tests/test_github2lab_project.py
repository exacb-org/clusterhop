# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Tests for authenticated GitLab project resolution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from clusterhop.github2lab.gitlab import HttpResponse, HttpTransportError
from clusterhop.github2lab.project import (
    GitLabProjectResolutionError,
    ResolvedGitLabProject,
    resolve_gitlab_project,
)

DUMMY_TOKEN = "dummy-project-token-that-must-not-appear"


@dataclass
class FakeTransport:
    response: HttpResponse | None = None
    error: HttpTransportError | None = None
    requests: list[dict[str, object]] = field(default_factory=list)

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _response(payload: object, status_code: int = 200) -> HttpResponse:
    return HttpResponse(
        status_code=status_code,
        headers={},
        body=json.dumps(payload).encode("utf-8"),
    )


def test_resolve_project_uses_encoded_path_and_private_token() -> None:
    transport = FakeTransport(
        response=_response(
            {
                "id": 8894,
                "path_with_namespace": "group/nested/project",
                "http_url_to_repo": "https://gitlab.example.test/group/nested/project.git",
            }
        )
    )

    project = resolve_gitlab_project(
        gitlab_url="https://gitlab.example.test/",
        project_identifier="group/nested/project",
        gitlab_token=DUMMY_TOKEN,
        transport=transport,
        request_timeout_seconds=7,
    )

    assert project == ResolvedGitLabProject(
        id=8894,
        path_with_namespace="group/nested/project",
        http_url_to_repo="https://gitlab.example.test/group/nested/project.git",
    )
    assert transport.requests == [
        {
            "method": "GET",
            "url": "https://gitlab.example.test/api/v4/projects/group%2Fnested%2Fproject",
            "headers": {"Accept": "application/json", "PRIVATE-TOKEN": DUMMY_TOKEN},
            "body": None,
            "timeout": 7.0,
        }
    ]


def test_resolve_project_normalizes_surrounding_gitlab_url_whitespace() -> None:
    transport = FakeTransport(
        response=_response(
            {
                "id": 8894,
                "path_with_namespace": "group/nested/project",
                "http_url_to_repo": "https://gitlab.example.test/group/nested/project.git",
            }
        )
    )

    resolve_gitlab_project(
        gitlab_url="  https://gitlab.example.test/  ",
        project_identifier="group/nested/project",
        gitlab_token=DUMMY_TOKEN,
        transport=transport,
    )

    assert transport.requests[0]["url"] == (
        "https://gitlab.example.test/api/v4/projects/group%2Fnested%2Fproject"
    )


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://other.example.test/group/project.git",
        "http://gitlab.example.test/group/project.git",
        "https://gitlab.example.test:8443/group/project.git",
        "https://user:password@gitlab.example.test/group/project.git",
    ],
)
def test_resolve_project_rejects_untrusted_clone_origin(remote_url: str) -> None:
    transport = FakeTransport(
        response=_response(
            {
                "id": 8894,
                "path_with_namespace": "group/project",
                "http_url_to_repo": remote_url,
            }
        )
    )

    with pytest.raises(GitLabProjectResolutionError, match="invalid project metadata"):
        resolve_gitlab_project(
            gitlab_url="https://gitlab.example.test",
            project_identifier="group/project",
            gitlab_token=DUMMY_TOKEN,
            transport=transport,
        )


@pytest.mark.parametrize(
    ("gitlab_url", "remote_url"),
    [
        (
            "https://gitlab.example.test",
            "https://gitlab.example.test/group/project.git",
        ),
        (
            "https://gitlab.example.test:443",
            "https://gitlab.example.test/group/project.git",
        ),
        (
            "http://gitlab.example.test:80",
            "http://gitlab.example.test/group/project.git",
        ),
    ],
)
def test_resolve_project_accepts_matching_effective_origin(
    gitlab_url: str,
    remote_url: str,
) -> None:
    transport = FakeTransport(
        response=_response(
            {
                "id": 8894,
                "path_with_namespace": "group/project",
                "http_url_to_repo": remote_url,
            }
        )
    )

    project = resolve_gitlab_project(
        gitlab_url=gitlab_url,
        project_identifier="group/project",
        gitlab_token=DUMMY_TOKEN,
        transport=transport,
    )

    assert project.http_url_to_repo == remote_url


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"id": True, "path_with_namespace": "group/project", "http_url_to_repo": "x"},
        {
            "id": 1,
            "path_with_namespace": "group/project",
            "http_url_to_repo": "https://user:password@gitlab.example.test/group/project.git",
        },
        {
            "id": 1,
            "path_with_namespace": "group/project",
            "http_url_to_repo": "ssh://git@gitlab.example.test/group/project.git",
        },
    ],
)
def test_invalid_project_metadata_is_rejected_safely(payload: object) -> None:
    transport = FakeTransport(response=_response(payload))

    with pytest.raises(GitLabProjectResolutionError, match="invalid project metadata") as captured:
        resolve_gitlab_project(
            gitlab_url="https://gitlab.example.test",
            project_identifier="group/project",
            gitlab_token=DUMMY_TOKEN,
            transport=transport,
        )

    assert DUMMY_TOKEN not in str(captured.value)


@pytest.mark.parametrize("status_code", [401, 403, 404, 500])
def test_http_failure_is_sanitized(status_code: int) -> None:
    transport = FakeTransport(
        response=HttpResponse(
            status_code=status_code,
            headers={},
            body=f"server echoed {DUMMY_TOKEN}".encode(),
        )
    )

    with pytest.raises(GitLabProjectResolutionError) as captured:
        resolve_gitlab_project(
            gitlab_url="https://gitlab.example.test",
            project_identifier="group/project",
            gitlab_token=DUMMY_TOKEN,
            transport=transport,
        )

    assert str(captured.value) == "Unable to resolve the GitLab project."
    assert DUMMY_TOKEN not in repr(captured.value)


def test_transport_failure_is_sanitized() -> None:
    transport = FakeTransport(error=HttpTransportError(f"network exposed {DUMMY_TOKEN}"))

    with pytest.raises(GitLabProjectResolutionError) as captured:
        resolve_gitlab_project(
            gitlab_url="https://gitlab.example.test",
            project_identifier="group/project",
            gitlab_token=DUMMY_TOKEN,
            transport=transport,
        )

    assert str(captured.value) == "Unable to resolve the GitLab project."
    assert DUMMY_TOKEN not in str(captured.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gitlab_url", ""),
        ("gitlab_url", "file:///tmp/gitlab"),
        ("gitlab_url", "https://user:password@gitlab.example.test"),
        ("project_identifier", ""),
        ("gitlab_token", ""),
        ("request_timeout_seconds", 0),
        ("request_timeout_seconds", True),
    ],
)
def test_invalid_resolution_inputs_make_no_request(field: str, value: object) -> None:
    values: dict[str, object] = {
        "gitlab_url": "https://gitlab.example.test",
        "project_identifier": "group/project",
        "gitlab_token": DUMMY_TOKEN,
        "request_timeout_seconds": 30.0,
    }
    values[field] = value
    transport = FakeTransport()

    with pytest.raises(GitLabProjectResolutionError):
        resolve_gitlab_project(transport=transport, **values)  # type: ignore[arg-type]

    assert transport.requests == []
