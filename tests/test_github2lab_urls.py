# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Tests for the shared GitLab credential-transport URL policy."""

from __future__ import annotations

import pytest

from clusterhop.github2lab.urls import (
    GitLabUrlError,
    HttpsOrigin,
    validate_git_remote_url,
    validate_gitlab_base_url,
    validate_https_request_url,
    validate_resolved_https_git_remote,
)


def test_gitlab_base_url_requires_https_and_normalizes_trailing_slash() -> None:
    assert validate_gitlab_base_url("  https://gitlab.example.test/group/  ") == (
        "https://gitlab.example.test/group"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://gitlab.example.test",
        "file:///tmp/gitlab",
        "https://user:password@gitlab.example.test",
        "https://gitlab.example.test?token=value",
        "https://gitlab.example.test#fragment",
        "https://gitlab.example.test\n.example.test",
        "https://gitlab.example.test:invalid",
    ],
)
def test_gitlab_base_url_rejects_insecure_or_ambiguous_values(url: str) -> None:
    with pytest.raises(GitLabUrlError):
        validate_gitlab_base_url(url)


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
            "https://gitlab.example.test",
            "https://gitlab.example.test:443/group/project.git",
        ),
        (
            "https://gitlab.example.test:8443",
            "https://gitlab.example.test:8443/group/project.git",
        ),
        (
            "https://[2001:db8::1]",
            "https://[2001:db8::1]/group/project.git",
        ),
    ],
)
def test_matching_https_git_remote_uses_effective_origin(
    gitlab_url: str,
    remote_url: str,
) -> None:
    remote = validate_git_remote_url(remote_url, gitlab_url)

    assert remote.kind == "https"
    assert remote.url == remote_url
    assert remote.https_origin is not None


@pytest.mark.parametrize(
    "remote_url",
    [
        "http://gitlab.example.test/group/project.git",
        "https://other.example.test/group/project.git",
        "https://gitlab.example.test:8443/group/project.git",
        "https://user:password@gitlab.example.test/group/project.git",
        "file:///tmp/project.git",
        "ext::malicious-command",
    ],
)
def test_git_remote_rejects_http_mismatched_credentials_and_unsupported_schemes(
    remote_url: str,
) -> None:
    with pytest.raises(GitLabUrlError):
        validate_git_remote_url(remote_url, "https://gitlab.example.test")


@pytest.mark.parametrize(
    "remote_url",
    [
        "git@gitlab.example.test:group/project.git",
        "ssh://git@gitlab.example.test/group/project.git",
        "ssh://git@gitlab.example.test:2222/group/project.git",
    ],
)
def test_matching_ssh_remotes_remain_supported_without_https_credentials(
    remote_url: str,
) -> None:
    remote = validate_git_remote_url(remote_url, "https://gitlab.example.test")

    assert remote.kind == "ssh"
    assert remote.https_origin is None


@pytest.mark.parametrize(
    "remote_url",
    [
        "git@other.example.test:group/project.git",
        "ssh://git@other.example.test/group/project.git",
        "ssh://git:password@gitlab.example.test/group/project.git",
    ],
)
def test_ssh_remote_must_use_matching_host_without_password(remote_url: str) -> None:
    with pytest.raises(GitLabUrlError):
        validate_git_remote_url(remote_url, "https://gitlab.example.test")


def test_project_resolved_remote_must_be_matching_https() -> None:
    with pytest.raises(GitLabUrlError):
        validate_resolved_https_git_remote(
            "git@gitlab.example.test:group/project.git",
            "https://gitlab.example.test",
        )


def test_request_urls_allow_queries_but_require_https() -> None:
    assert validate_https_request_url("https://gitlab.example.test/api/v4/jobs?page=2").endswith(
        "?page=2"
    )
    with pytest.raises(GitLabUrlError):
        validate_https_request_url("http://gitlab.example.test/api/v4/jobs?page=2")


def test_https_origin_formats_git_credential_hosts_for_ports_and_ipv6() -> None:
    default = HttpsOrigin("gitlab.example.test", 443)
    custom_ipv6 = HttpsOrigin("2001:db8::1", 8443)

    assert default.credential_host == "gitlab.example.test"
    assert default.credential_host_with_explicit_default_port == "gitlab.example.test:443"
    assert custom_ipv6.credential_host == "[2001:db8::1]:8443"
    assert custom_ipv6.credential_host_with_explicit_default_port == ""
