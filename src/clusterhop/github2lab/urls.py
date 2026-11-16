# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Shared security policy for credential-bearing GitLab URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import SplitResult, urlsplit

_SCP_STYLE_SSH_URL = re.compile(
    r"^(?:(?P<username>[A-Za-z0-9._-]+)@)?"
    r"(?P<hostname>\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+):"
    r"(?P<path>[^\s]+)$"
)


class GitLabUrlError(ValueError):
    """Raised when a GitLab URL violates the credential-transport policy."""


@dataclass(frozen=True)
class HttpsOrigin:
    """Normalized HTTPS origin used for exact trust-boundary comparisons."""

    hostname: str
    port: int

    @property
    def credential_url(self) -> str:
        """Return the Git credential configuration URL for this origin."""

        host = f"[{self.hostname}]" if ":" in self.hostname else self.hostname
        port = "" if self.port == 443 else f":{self.port}"
        return f"https://{host}{port}"

    @property
    def credential_host(self) -> str:
        """Return the host value expected from Git's credential protocol."""

        host = f"[{self.hostname}]" if ":" in self.hostname else self.hostname
        port = "" if self.port == 443 else f":{self.port}"
        return f"{host}{port}"

    @property
    def credential_host_with_explicit_default_port(self) -> str:
        """Return the alternate Git host form for an explicit default port."""

        if self.port != 443:
            return ""
        host = f"[{self.hostname}]" if ":" in self.hostname else self.hostname
        return f"{host}:443"


@dataclass(frozen=True)
class ValidatedGitRemote:
    """Validated Git remote information required by the push implementation."""

    url: str
    kind: Literal["https", "ssh"]
    https_origin: HttpsOrigin | None = None


def validate_gitlab_base_url(value: object) -> str:
    """Return a normalized HTTPS GitLab base URL."""

    parsed, normalized = _parse_https_url(value)
    if parsed.query or parsed.fragment:
        raise GitLabUrlError("GitLab URL must not contain a query or fragment.")
    return normalized.rstrip("/")


def validate_https_request_url(value: object) -> str:
    """Validate one credential-bearing HTTPS request URL."""

    parsed, normalized = _parse_https_url(value)
    if parsed.fragment:
        raise GitLabUrlError("GitLab request URL must not contain a fragment.")
    return normalized


def validate_git_remote_url(value: object, gitlab_url: object) -> ValidatedGitRemote:
    """Validate an HTTPS or SSH Git remote against the configured GitLab server."""

    base_url = validate_gitlab_base_url(gitlab_url)
    base_origin = https_origin(base_url)
    if not isinstance(value, str) or not value.strip():
        raise GitLabUrlError("GitLab remote URL is invalid.")
    normalized = value.strip()
    if _contains_unsafe_characters(normalized):
        raise GitLabUrlError("GitLab remote URL is invalid.")

    parsed = _safe_urlsplit(normalized)
    if parsed.scheme.casefold() in {"http", "https"}:
        remote_url = validate_https_request_url(normalized)
        remote = _safe_urlsplit(remote_url)
        if remote.query or remote.fragment or not remote.path or remote.path == "/":
            raise GitLabUrlError("GitLab remote URL is invalid.")
        remote_origin = _origin_from_parsed(remote)
        if remote_origin != base_origin:
            raise GitLabUrlError("GitLab remote URL must use the configured GitLab origin.")
        return ValidatedGitRemote(remote_url, "https", remote_origin)

    if parsed.scheme:
        if parsed.scheme.casefold() != "ssh":
            raise GitLabUrlError("GitLab remote URL uses an unsupported scheme.")
        if (
            parsed.hostname is None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path
            or parsed.path == "/"
        ):
            raise GitLabUrlError("GitLab SSH remote URL is invalid.")
        _validated_port(parsed)
        if parsed.hostname.casefold() != base_origin.hostname:
            raise GitLabUrlError("GitLab SSH remote host must match the GitLab server.")
        return ValidatedGitRemote(normalized, "ssh")

    match = _SCP_STYLE_SSH_URL.fullmatch(normalized)
    if match is None or not match.group("path"):
        raise GitLabUrlError("GitLab remote URL uses an unsupported scheme.")
    hostname = match.group("hostname").removeprefix("[").removesuffix("]").casefold()
    if hostname != base_origin.hostname:
        raise GitLabUrlError("GitLab SSH remote host must match the GitLab server.")
    return ValidatedGitRemote(normalized, "ssh")


def validate_resolved_https_git_remote(value: object, gitlab_url: object) -> str:
    """Validate a project-resolved remote, which must always be HTTPS."""

    remote = validate_git_remote_url(value, gitlab_url)
    if remote.kind != "https":
        raise GitLabUrlError("GitLab returned a non-HTTPS project remote URL.")
    return remote.url


def https_origin(value: object) -> HttpsOrigin:
    """Return the normalized origin of a validated HTTPS URL."""

    parsed, _ = _parse_https_url(value)
    return _origin_from_parsed(parsed)


def _parse_https_url(value: object) -> tuple[SplitResult, str]:
    if not isinstance(value, str) or not value.strip():
        raise GitLabUrlError("GitLab URL is invalid.")
    normalized = value.strip()
    if _contains_unsafe_characters(normalized):
        raise GitLabUrlError("GitLab URL is invalid.")
    parsed = _safe_urlsplit(normalized)
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise GitLabUrlError("GitLab URL must use HTTPS without embedded credentials.")
    _validated_port(parsed)
    return parsed, normalized


def _origin_from_parsed(parsed: SplitResult) -> HttpsOrigin:
    assert parsed.hostname is not None
    return HttpsOrigin(parsed.hostname.casefold(), _validated_port(parsed))


def _validated_port(parsed: SplitResult) -> int:
    try:
        port = parsed.port
    except ValueError:
        raise GitLabUrlError("GitLab URL has an invalid port.") from None
    if port is not None and port <= 0:
        raise GitLabUrlError("GitLab URL has an invalid port.")
    return 443 if port is None else port


def _safe_urlsplit(value: str) -> SplitResult:
    try:
        return urlsplit(value)
    except ValueError:
        raise GitLabUrlError("GitLab URL is invalid.") from None


def _contains_unsafe_characters(value: str) -> bool:
    return any(character.isspace() or ord(character) < 32 for character in value)
