# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Unit tests for the GitLab API client."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest

from clusterhop.github2lab.config import GitHub2LabConfig
from clusterhop.github2lab.gitlab import (
    JOBS_PER_PAGE,
    GitLabApiError,
    GitLabClient,
    HttpResponse,
    HttpTransportError,
)
from clusterhop.github2lab.models import GitLabJob, GitLabPipeline

DUMMY_GITLAB_TOKEN = "dummy-private-token"
DUMMY_TRIGGER_TOKEN = "dummy-trigger-token"


@dataclass(frozen=True)
class RecordedRequest:
    """A request captured by the fake transport."""

    method: str
    url: str
    headers: Mapping[str, str] = field(repr=False)
    body: bytes | None = field(repr=False)
    timeout: float


class FakeTransport:
    """Deterministic transport that never performs network I/O."""

    def __init__(
        self,
        responses: list[HttpResponse] | None = None,
        *,
        error: HttpTransportError | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._error = error
        self.requests: list[RecordedRequest] = []

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
            RecordedRequest(
                method=method,
                url=url,
                headers=dict(headers),
                body=body,
                timeout=timeout,
            )
        )
        if self._error is not None:
            raise self._error
        if not self._responses:
            raise AssertionError("The fake transport has no queued response.")
        return self._responses.pop(0)


def _config(**overrides: object) -> GitHub2LabConfig:
    values: dict[str, object] = {
        "gitlab_token": DUMMY_GITLAB_TOKEN,
        "gitlab_trigger_token": DUMMY_TRIGGER_TOKEN,
        "gitlab_url": "https://gitlab.example.test/",
        "gitlab_remote_url": "git@gitlab.example.test:group/project.git",
        "gitlab_project_id": "1234",
        "gitlab_project_path": None,
        "github_commit_sha": "abc123def456",
        "github_repository": "example/source-project",
        "github_run_id": "987654",
        "extra_pipeline_variables": {},
    }
    values.update(overrides)
    return GitHub2LabConfig(**values)  # type: ignore[arg-type]


def _json_response(
    payload: object,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    return HttpResponse(
        status_code=status_code,
        headers=dict(headers or {}),
        body=json.dumps(payload).encode("utf-8"),
    )


def _pipeline_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": 42,
        "status": "running",
        "web_url": "https://gitlab.example.test/group/project/-/pipelines/42",
        "ref": "ci/abc123def456",
        "sha": "abc123def456",
    }
    payload.update(overrides)
    return payload


def _job_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": 101,
        "name": "test",
        "status": "success",
        "stage": "test",
        "duration": 3.5,
        "web_url": "https://gitlab.example.test/group/project/-/jobs/101",
    }
    payload.update(overrides)
    return payload


def test_client_repr_does_not_expose_tokens() -> None:
    client = GitLabClient(_config(), transport=FakeTransport())

    representation = repr(client)

    assert representation == "GitLabClient()"
    assert DUMMY_GITLAB_TOKEN not in representation
    assert DUMMY_TRIGGER_TOKEN not in representation


def test_trigger_pipeline_sends_form_data_and_returns_pipeline() -> None:
    transport = FakeTransport([_json_response(_pipeline_payload(), status_code=201)])
    client = GitLabClient(
        _config(extra_pipeline_variables={"CUSTOM_VARIABLE": "custom value"}),
        transport=transport,
        request_timeout_seconds=12.0,
    )

    pipeline = client.trigger_pipeline("ci/abc123def456")

    assert pipeline == GitLabPipeline(
        id=42,
        status="running",
        web_url="https://gitlab.example.test/group/project/-/pipelines/42",
        ref="ci/abc123def456",
        sha="abc123def456",
    )
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == "https://gitlab.example.test/api/v4/projects/1234/trigger/pipeline"
    assert request.timeout == 12.0
    assert request.headers == {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    assert request.body is not None
    form = parse_qs(request.body.decode("utf-8"), keep_blank_values=True)
    assert form == {
        "token": [DUMMY_TRIGGER_TOKEN],
        "ref": ["ci/abc123def456"],
        "variables[GITHUB_COMMIT_SHA]": ["abc123def456"],
        "variables[GITHUB_REPOSITORY]": ["example/source-project"],
        "variables[GITHUB_RUN_ID]": ["987654"],
        "variables[CUSTOM_VARIABLE]": ["custom value"],
    }
    assert "PRIVATE-TOKEN" not in request.headers


@pytest.mark.parametrize(
    "reserved_name",
    ["GITHUB_COMMIT_SHA", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"],
)
def test_trigger_pipeline_rejects_reserved_variable_overrides(reserved_name: str) -> None:
    transport = FakeTransport()
    client = GitLabClient(
        _config(extra_pipeline_variables={reserved_name: "replacement"}),
        transport=transport,
    )

    with pytest.raises(ValueError, match="must not override"):
        client.trigger_pipeline("ci/abc123def456")

    assert transport.requests == []


def test_project_path_is_percent_encoded() -> None:
    transport = FakeTransport([_json_response(_pipeline_payload())])
    client = GitLabClient(
        _config(gitlab_project_id=None, gitlab_project_path="group/nested/project"),
        transport=transport,
    )

    client.get_pipeline(42)

    assert transport.requests[0].url.startswith(
        "https://gitlab.example.test/api/v4/projects/group%2Fnested%2Fproject/"
    )


def test_get_pipeline_uses_private_token_and_parses_optional_fields() -> None:
    transport = FakeTransport(
        [_json_response(_pipeline_payload(web_url=None, ref=None, sha=None, status="success"))]
    )
    client = GitLabClient(_config(), transport=transport)

    pipeline = client.get_pipeline(42)

    assert pipeline == GitLabPipeline(
        id=42,
        status="success",
        web_url=None,
        ref=None,
        sha=None,
    )
    request = transport.requests[0]
    assert request.method == "GET"
    assert request.url == "https://gitlab.example.test/api/v4/projects/1234/pipelines/42"
    assert request.headers["PRIVATE-TOKEN"] == DUMMY_GITLAB_TOKEN
    assert request.body is None


def test_get_pipeline_jobs_follows_pagination() -> None:
    first_job = _job_payload()
    second_job = _job_payload(
        id=102,
        name="package",
        stage="build",
        duration=None,
        web_url=None,
    )
    transport = FakeTransport(
        [
            _json_response([first_job], headers={"x-next-page": "2"}),
            _json_response([second_job], headers={"X-Next-Page": ""}),
        ]
    )
    client = GitLabClient(_config(), transport=transport)

    jobs = client.get_pipeline_jobs(42)

    assert jobs == (
        GitLabJob(
            id=101,
            name="test",
            status="success",
            stage="test",
            duration=3.5,
            web_url="https://gitlab.example.test/group/project/-/jobs/101",
        ),
        GitLabJob(
            id=102,
            name="package",
            status="success",
            stage="build",
            duration=None,
            web_url=None,
        ),
    )
    assert len(transport.requests) == 2
    assert parse_qs(urlsplit(transport.requests[0].url).query) == {
        "page": ["1"],
        "per_page": [str(JOBS_PER_PAGE)],
    }
    assert parse_qs(urlsplit(transport.requests[1].url).query) == {
        "page": ["2"],
        "per_page": [str(JOBS_PER_PAGE)],
    }
    assert all(
        request.headers["PRIVATE-TOKEN"] == DUMMY_GITLAB_TOKEN for request in transport.requests
    )


def test_get_job_trace_returns_unmodified_bytes() -> None:
    trace = b"first line\ninvalid utf-8: \xff\n"
    transport = FakeTransport([HttpResponse(status_code=200, headers={}, body=trace)])
    client = GitLabClient(_config(), transport=transport)

    result = client.get_job_trace(101)

    assert result == trace
    request = transport.requests[0]
    assert request.method == "GET"
    assert request.url == "https://gitlab.example.test/api/v4/projects/1234/jobs/101/trace"
    assert request.headers["PRIVATE-TOKEN"] == DUMMY_GITLAB_TOKEN
    assert request.headers["Accept"] == "text/plain"


def test_cancel_pipeline_posts_and_returns_pipeline() -> None:
    transport = FakeTransport(
        [_json_response(_pipeline_payload(status="canceled"), status_code=200)]
    )
    client = GitLabClient(_config(), transport=transport)

    pipeline = client.cancel_pipeline(42)

    assert pipeline.status == "canceled"
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == "https://gitlab.example.test/api/v4/projects/1234/pipelines/42/cancel"
    assert request.headers["PRIVATE-TOKEN"] == DUMMY_GITLAB_TOKEN
    assert request.body == b""


def test_cancel_pipeline_uses_bounded_timeout_override() -> None:
    transport = FakeTransport(
        [_json_response(_pipeline_payload(status="canceled"), status_code=200)]
    )
    client = GitLabClient(_config(), transport=transport, request_timeout_seconds=30.0)

    client.cancel_pipeline(42, timeout_seconds=7.5)

    assert transport.requests[0].timeout == 7.5


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_cancel_pipeline_rejects_invalid_timeout_override(timeout: object) -> None:
    client = GitLabClient(_config(), transport=FakeTransport())

    with pytest.raises(ValueError, match="greater than 0"):
        client.cancel_pipeline(42, timeout_seconds=timeout)  # type: ignore[arg-type]


@pytest.mark.parametrize("status_code", [401, 403, 404, 500])
def test_http_errors_are_sanitized(status_code: int) -> None:
    sensitive_response = {
        "message": f"server echoed {DUMMY_GITLAB_TOKEN} and {DUMMY_TRIGGER_TOKEN}"
    }
    transport = FakeTransport([_json_response(sensitive_response, status_code=status_code)])
    client = GitLabClient(_config(), transport=transport)

    with pytest.raises(GitLabApiError) as captured:
        client.get_pipeline(42)

    error = captured.value
    assert error.operation == "get pipeline"
    assert error.status_code == status_code
    assert f"HTTP {status_code}" in str(error)
    assert DUMMY_GITLAB_TOKEN not in str(error)
    assert DUMMY_TRIGGER_TOKEN not in str(error)
    assert DUMMY_GITLAB_TOKEN not in repr(error)
    assert DUMMY_TRIGGER_TOKEN not in repr(error)


def test_transport_errors_are_converted_to_sanitized_api_errors() -> None:
    transport = FakeTransport(
        error=HttpTransportError(
            f"network failure involving {DUMMY_GITLAB_TOKEN} and {DUMMY_TRIGGER_TOKEN}"
        )
    )
    client = GitLabClient(_config(), transport=transport)

    with pytest.raises(GitLabApiError) as captured:
        client.get_pipeline(42)

    error_text = str(captured.value)
    assert error_text == "GitLab API operation get pipeline failed."
    assert DUMMY_GITLAB_TOKEN not in error_text
    assert DUMMY_TRIGGER_TOKEN not in error_text


@pytest.mark.parametrize(
    "response",
    [
        HttpResponse(status_code=200, headers={}, body=b"not-json"),
        _json_response([]),
        _json_response({"id": 42}),
        _json_response(_pipeline_payload(id=True)),
        _json_response(_pipeline_payload(status="")),
        _json_response(_pipeline_payload(web_url=123)),
    ],
)
def test_invalid_pipeline_responses_raise_sanitized_error(response: HttpResponse) -> None:
    client = GitLabClient(_config(), transport=FakeTransport([response]))

    with pytest.raises(GitLabApiError, match="invalid response") as captured:
        client.get_pipeline(42)

    assert DUMMY_GITLAB_TOKEN not in str(captured.value)
    assert DUMMY_TRIGGER_TOKEN not in str(captured.value)


@pytest.mark.parametrize(
    "jobs_payload",
    [
        {},
        ["not-an-object"],
        [_job_payload(id=0)],
        [_job_payload(duration="unknown")],
    ],
)
def test_invalid_job_responses_raise_api_error(jobs_payload: object) -> None:
    client = GitLabClient(
        _config(),
        transport=FakeTransport([_json_response(jobs_payload)]),
    )

    with pytest.raises(GitLabApiError, match="invalid response"):
        client.get_pipeline_jobs(42)


@pytest.mark.parametrize("next_page", ["not-a-number", "0", "-1", "1"])
def test_invalid_jobs_pagination_raises_api_error(next_page: str) -> None:
    transport = FakeTransport(
        [
            _json_response([], headers={"X-Next-Page": next_page}),
            _json_response([], headers={}),
        ]
    )
    client = GitLabClient(_config(), transport=transport)

    with pytest.raises(GitLabApiError, match="invalid jobs pagination"):
        client.get_pipeline_jobs(42)


@pytest.mark.parametrize("identifier", [0, -1, True, 1.5, "42"])
def test_api_methods_reject_invalid_identifiers(identifier: object) -> None:
    client = GitLabClient(_config(), transport=FakeTransport())

    with pytest.raises(ValueError, match="positive integer"):
        client.get_pipeline(identifier)  # type: ignore[arg-type]


@pytest.mark.parametrize("ref", ["", "   "])
def test_trigger_pipeline_rejects_blank_ref(ref: str) -> None:
    client = GitLabClient(_config(), transport=FakeTransport())

    with pytest.raises(ValueError, match="non-empty string"):
        client.trigger_pipeline(ref)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_client_rejects_invalid_request_timeout(timeout: object) -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        GitLabClient(
            _config(),
            transport=FakeTransport(),
            request_timeout_seconds=timeout,  # type: ignore[arg-type]
        )
