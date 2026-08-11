# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""GitLab API operations for the GitHub-to-GitLab bridge."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from clusterhop.github2lab.config import GitHub2LabConfig
from clusterhop.github2lab.models import GitLabJob, GitLabPipeline

DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
JOBS_PER_PAGE = 100
_RESERVED_TRIGGER_VARIABLES = frozenset(
    {
        "GITHUB_COMMIT_SHA",
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ID",
    }
)


class GitLabApiError(RuntimeError):
    """A sanitized failure from a GitLab API operation."""

    def __init__(
        self,
        operation: str,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.status_code = status_code


class HttpTransportError(RuntimeError):
    """A sanitized network failure raised by an HTTP transport."""


@dataclass(frozen=True)
class HttpResponse:
    """The transport response fields required by the GitLab client."""

    status_code: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)


class HttpTransport(Protocol):
    """Minimal injectable HTTP transport used by the GitLab client."""

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        """Send one HTTP request and return its response."""


class UrllibHttpTransport:
    """Standard-library HTTP transport for GitLab API requests."""

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        """Send a request without exposing request details in failures."""

        request = Request(url=url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except HTTPError as error:
            response_headers = dict(error.headers.items()) if error.headers is not None else {}
            return HttpResponse(
                status_code=error.code,
                headers=response_headers,
                body=error.read(),
            )
        except (OSError, URLError, ValueError):
            raise HttpTransportError("GitLab HTTP request failed.") from None


class GitLabClient:
    """Reusable client for the GitLab endpoints needed by github2lab."""

    def __init__(
        self,
        config: GitHub2LabConfig,
        *,
        transport: HttpTransport | None = None,
        request_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, (int, float))
            or not math.isfinite(request_timeout_seconds)
            or request_timeout_seconds <= 0
        ):
            raise ValueError("request_timeout_seconds must be greater than 0.")

        project_identifier = config.gitlab_project_id or config.gitlab_project_path
        assert project_identifier is not None

        self._config = config
        self._transport = transport if transport is not None else UrllibHttpTransport()
        self._request_timeout_seconds = float(request_timeout_seconds)
        api_base_url = f"{config.gitlab_url.rstrip('/')}/api/v4"
        encoded_project = quote(project_identifier, safe="")
        self._project_api_url = f"{api_base_url}/projects/{encoded_project}"

    def __repr__(self) -> str:
        """Return a representation that cannot expose configuration secrets."""

        return f"{type(self).__name__}()"

    def trigger_pipeline(self, ref: str) -> GitLabPipeline:
        """Trigger a pipeline for a GitLab branch or tag."""

        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("ref must be a non-empty string.")

        if _RESERVED_TRIGGER_VARIABLES.intersection(self._config.extra_pipeline_variables):
            raise ValueError(
                "Extra pipeline variables must not override GitHub metadata variables."
            )

        variables = {
            "GITHUB_COMMIT_SHA": self._config.github_commit_sha,
            "GITHUB_REPOSITORY": self._config.github_repository,
            "GITHUB_RUN_ID": self._config.github_run_id,
            **self._config.extra_pipeline_variables,
        }
        form_fields = [
            ("token", self._config.gitlab_trigger_token),
            ("ref", ref),
            *[(f"variables[{key}]", value) for key, value in variables.items()],
        ]
        body = urlencode(form_fields).encode("utf-8")
        response = self._request(
            operation="trigger pipeline",
            method="POST",
            url=f"{self._project_api_url}/trigger/pipeline",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=body,
            expected_status=201,
        )
        return _parse_pipeline(
            _decode_json_object(response, "trigger pipeline"), "trigger pipeline"
        )

    def get_pipeline(self, pipeline_id: int) -> GitLabPipeline:
        """Retrieve the current details of one GitLab pipeline."""

        _validate_positive_identifier("pipeline_id", pipeline_id)
        operation = "get pipeline"
        response = self._request(
            operation=operation,
            method="GET",
            url=f"{self._project_api_url}/pipelines/{pipeline_id}",
            headers=self._private_token_headers(),
            body=None,
            expected_status=200,
        )
        return _parse_pipeline(_decode_json_object(response, operation), operation)

    def get_pipeline_jobs(self, pipeline_id: int) -> tuple[GitLabJob, ...]:
        """Retrieve every page of jobs belonging to a GitLab pipeline."""

        _validate_positive_identifier("pipeline_id", pipeline_id)
        operation = "get pipeline jobs"
        jobs: list[GitLabJob] = []
        page = 1
        seen_pages: set[int] = set()

        while True:
            if page in seen_pages:
                raise GitLabApiError(operation, "GitLab returned invalid jobs pagination.")
            seen_pages.add(page)

            query = urlencode({"page": page, "per_page": JOBS_PER_PAGE})
            response = self._request(
                operation=operation,
                method="GET",
                url=f"{self._project_api_url}/pipelines/{pipeline_id}/jobs?{query}",
                headers=self._private_token_headers(),
                body=None,
                expected_status=200,
            )
            payload = _decode_json(response, operation)
            if not isinstance(payload, list):
                raise _invalid_response_error(operation)
            jobs.extend(_parse_job(item, operation) for item in payload)

            next_page_header = _response_header(response.headers, "X-Next-Page")
            if not next_page_header:
                break
            try:
                next_page = int(next_page_header)
            except ValueError:
                raise GitLabApiError(
                    operation, "GitLab returned invalid jobs pagination."
                ) from None
            if next_page <= 0:
                raise GitLabApiError(operation, "GitLab returned invalid jobs pagination.")
            page = next_page

        return tuple(jobs)

    def get_job_trace(self, job_id: int) -> bytes:
        """Retrieve a GitLab job trace without altering its bytes."""

        _validate_positive_identifier("job_id", job_id)
        response = self._request(
            operation="get job trace",
            method="GET",
            url=f"{self._project_api_url}/jobs/{job_id}/trace",
            headers={**self._private_token_headers(), "Accept": "text/plain"},
            body=None,
            expected_status=200,
        )
        return response.body

    def cancel_pipeline(self, pipeline_id: int) -> GitLabPipeline:
        """Request cancellation of all jobs in a GitLab pipeline."""

        _validate_positive_identifier("pipeline_id", pipeline_id)
        operation = "cancel pipeline"
        response = self._request(
            operation=operation,
            method="POST",
            url=f"{self._project_api_url}/pipelines/{pipeline_id}/cancel",
            headers=self._private_token_headers(),
            body=b"",
            expected_status=200,
        )
        return _parse_pipeline(_decode_json_object(response, operation), operation)

    def _private_token_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "PRIVATE-TOKEN": self._config.gitlab_token,
        }

    def _request(
        self,
        *,
        operation: str,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        expected_status: int,
    ) -> HttpResponse:
        try:
            response = self._transport.request(
                method=method,
                url=url,
                headers=headers,
                body=body,
                timeout=self._request_timeout_seconds,
            )
        except HttpTransportError:
            raise GitLabApiError(operation, f"GitLab API operation {operation} failed.") from None

        if response.status_code != expected_status:
            raise GitLabApiError(
                operation,
                _http_error_message(operation, response.status_code),
                status_code=response.status_code,
            )
        return response


def _validate_positive_identifier(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")


def _decode_json(response: HttpResponse, operation: str) -> object:
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _invalid_response_error(operation) from None


def _decode_json_object(response: HttpResponse, operation: str) -> Mapping[str, object]:
    payload = _decode_json(response, operation)
    if not isinstance(payload, dict):
        raise _invalid_response_error(operation)
    return cast(dict[str, object], payload)


def _parse_pipeline(payload: Mapping[str, object], operation: str) -> GitLabPipeline:
    return GitLabPipeline(
        id=_required_integer(payload, "id", operation),
        status=_required_string(payload, "status", operation),
        web_url=_optional_string(payload, "web_url", operation),
        ref=_optional_string(payload, "ref", operation),
        sha=_optional_string(payload, "sha", operation),
    )


def _parse_job(payload: object, operation: str) -> GitLabJob:
    if not isinstance(payload, dict):
        raise _invalid_response_error(operation)
    job = cast(dict[str, object], payload)
    return GitLabJob(
        id=_required_integer(job, "id", operation),
        name=_required_string(job, "name", operation),
        status=_required_string(job, "status", operation),
        stage=_required_string(job, "stage", operation),
        duration=_optional_number(job, "duration", operation),
        web_url=_optional_string(job, "web_url", operation),
    )


def _required_integer(payload: Mapping[str, object], name: str, operation: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _invalid_response_error(operation)
    return value


def _required_string(payload: Mapping[str, object], name: str, operation: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise _invalid_response_error(operation)
    return value


def _optional_string(
    payload: Mapping[str, object],
    name: str,
    operation: str,
) -> str | None:
    value = payload.get(name)
    if value is not None and not isinstance(value, str):
        raise _invalid_response_error(operation)
    return value


def _optional_number(
    payload: Mapping[str, object],
    name: str,
    operation: str,
) -> float | None:
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _invalid_response_error(operation)
    return float(value)


def _invalid_response_error(operation: str) -> GitLabApiError:
    return GitLabApiError(operation, f"GitLab returned an invalid response for {operation}.")


def _http_error_message(operation: str, status_code: int) -> str:
    if status_code == 401:
        explanation = "authentication failed"
    elif status_code == 403:
        explanation = "access was denied"
    elif status_code == 404:
        explanation = "the resource was not found"
    else:
        explanation = "the request was unsuccessful"
    return f"GitLab API operation {operation} failed with HTTP {status_code}: {explanation}."


def _response_header(headers: Mapping[str, str], name: str) -> str | None:
    expected_name = name.casefold()
    for header_name, value in headers.items():
        if header_name.casefold() == expected_name:
            return value.strip()
    return None
