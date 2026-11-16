# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Git operations for transferring GitHub commits to GitLab."""

from __future__ import annotations

import math
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from clusterhop.github2lab.config import GitHub2LabConfig
from clusterhop.github2lab.urls import GitLabUrlError, ValidatedGitRemote, validate_git_remote_url

DEFAULT_GIT_COMMAND_TIMEOUT_SECONDS = 120.0
_COMMIT_SHA_PATTERN = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_GITLAB_TOKEN_ENVIRONMENT_VARIABLE = "CLUSTERHOP_GITLAB_GIT_TOKEN"
_GITLAB_HOST_ENVIRONMENT_VARIABLE = "CLUSTERHOP_GITLAB_GIT_HOST"
_GITLAB_ALTERNATE_HOST_ENVIRONMENT_VARIABLE = "CLUSTERHOP_GITLAB_GIT_ALTERNATE_HOST"
_KEYLOOM_PASSWORD_ENVIRONMENT_VARIABLE = "KEYLOOM_PASSWORD"
_UNSAFE_GIT_ENVIRONMENT_VARIABLES = frozenset(
    {
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG",
        "GIT_SSL_NO_VERIFY",
        "GIT_CURL_VERBOSE",
        "GIT_REDIRECT_STDERR",
        "GIT_EXEC_PATH",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
    }
)
_HTTP_CREDENTIAL_HELPER = (
    "!f() { "
    'if test "$1" = get; then '
    "protocol=; host=; "
    "while IFS='=' read -r key value; do "
    'case "$key" in protocol) protocol=$value ;; host) host=$value ;; esac; '
    "done; "
    'if test "$protocol" = https && '
    f'{{ test "$host" = "${_GITLAB_HOST_ENVIRONMENT_VARIABLE}" || '
    f'{{ test -n "${_GITLAB_ALTERNATE_HOST_ENVIRONMENT_VARIABLE}" && '
    f'test "$host" = "${_GITLAB_ALTERNATE_HOST_ENVIRONMENT_VARIABLE}"; }}; }}; then '
    'printf "%s\\n" "username=oauth2" '
    f'"password=${_GITLAB_TOKEN_ENVIRONMENT_VARIABLE}"; '
    "fi; "
    "fi; "
    "}; f"
)


class GitPushError(RuntimeError):
    """A sanitized failure while preparing or pushing the GitLab CI branch."""


class GitCommandExecutionError(RuntimeError):
    """A sanitized failure to execute a Git command."""


@dataclass(frozen=True)
class GitCommandResult:
    """Output required by Git operations without exposing it in representations."""

    return_code: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)


class GitCommandExecutor(Protocol):
    """Injectable interface for running Git without a shell."""

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
    ) -> GitCommandResult:
        """Run one Git command and capture its result."""


class SubprocessGitCommandExecutor:
    """Production Git executor based on argument-safe subprocess execution."""

    def __init__(self, *, timeout_seconds: float = DEFAULT_GIT_COMMAND_TIMEOUT_SECONDS) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be greater than 0.")
        self._timeout_seconds = float(timeout_seconds)

    def __repr__(self) -> str:
        """Return a representation containing no command or environment data."""

        return f"{type(self).__name__}(timeout_seconds={self._timeout_seconds!r})"

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
    ) -> GitCommandResult:
        """Run a command without a shell and sanitize execution failures."""

        process_environment = {
            name: value
            for name, value in os.environ.items()
            if not _is_unsafe_inherited_git_environment_variable(name)
        }
        for secret_name in (
            _KEYLOOM_PASSWORD_ENVIRONMENT_VARIABLE,
            "GITLAB_TOKEN",
            "GITLAB_TRIGGER_TOKEN",
            _GITLAB_TOKEN_ENVIRONMENT_VARIABLE,
        ):
            process_environment.pop(secret_name, None)
        process_environment.update(environment)
        for secret_name in (
            _KEYLOOM_PASSWORD_ENVIRONMENT_VARIABLE,
            "GITLAB_TOKEN",
            "GITLAB_TRIGGER_TOKEN",
        ):
            process_environment.pop(secret_name, None)

        try:
            completed = subprocess.run(
                tuple(arguments),
                cwd=cwd,
                env=process_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            raise GitCommandExecutionError("Git command execution failed.") from None

        return GitCommandResult(
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class GitLabBranchPusher:
    """Transfer the checked-out GitHub commit to its GitLab CI branch."""

    def __init__(
        self,
        config: GitHub2LabConfig,
        *,
        executor: GitCommandExecutor | None = None,
        repository_path: str | Path | None = None,
    ) -> None:
        self._config = config
        self._executor = executor if executor is not None else SubprocessGitCommandExecutor()
        self._repository_path = (
            Path.cwd() if repository_path is None else Path(repository_path).expanduser()
        )

    def __repr__(self) -> str:
        """Return a representation that cannot expose configuration secrets."""

        return f"{type(self).__name__}()"

    @property
    def branch_name(self) -> str:
        """Return the remote CI branch derived from the configured GitHub commit."""

        return create_ci_branch_name(self._config.github_commit_sha)

    def push_current_commit(self) -> str:
        """Verify and push HEAD to GitLab with automatic pipeline creation disabled."""

        branch_name = self.branch_name
        head_result = self._run_checked(
            ("git", "rev-parse", "--verify", "HEAD^{commit}"),
            operation="verify the current Git commit",
            environment=_base_git_environment(),
        )
        current_commit = head_result.stdout.strip()
        if not _COMMIT_SHA_PATTERN.fullmatch(current_commit):
            raise GitPushError("Git returned an invalid commit identifier for HEAD.")
        if current_commit.casefold() != self._config.github_commit_sha.casefold():
            raise GitPushError("Current Git HEAD does not match github_commit_sha.")

        try:
            validated_remote = validate_git_remote_url(
                self._config.gitlab_remote_url,
                self._config.gitlab_url,
            )
        except GitLabUrlError:
            raise GitPushError("The GitLab remote URL is invalid.") from None
        effective_remote_result = self._run_checked(
            ("git", "ls-remote", "--get-url", "--", validated_remote.url),
            operation="verify the effective GitLab remote URL",
            environment=_base_git_environment(),
        )
        effective_remote = effective_remote_result.stdout.strip()
        if effective_remote != validated_remote.url:
            raise GitPushError("Git configuration rewrote the GitLab remote URL.")

        remote_ref = f"refs/heads/{branch_name}"
        push_environment = _push_environment(self._config, validated_remote)
        self._run_checked(
            (
                "git",
                "push",
                "--push-option=ci.skip",
                "--",
                validated_remote.url,
                f"HEAD:{remote_ref}",
            ),
            operation="push the GitLab CI branch",
            environment=push_environment,
        )
        return branch_name

    def _run_checked(
        self,
        arguments: Sequence[str],
        *,
        operation: str,
        environment: Mapping[str, str],
    ) -> GitCommandResult:
        try:
            result = self._executor.run(
                arguments,
                cwd=self._repository_path,
                environment=environment,
            )
        except GitCommandExecutionError:
            raise GitPushError(f"Unable to {operation}.") from None

        if result.return_code != 0:
            raise GitPushError(
                f"Unable to {operation}: Git exited with status {result.return_code}."
            )
        return result


def create_ci_branch_name(github_commit_sha: str) -> str:
    """Create a safe GitLab CI branch name from a full Git commit identifier."""

    if not isinstance(github_commit_sha, str) or not _COMMIT_SHA_PATTERN.fullmatch(
        github_commit_sha
    ):
        raise GitPushError("github_commit_sha must be a 40- or 64-character hexadecimal value.")
    return f"ci/{github_commit_sha}"


def _base_git_environment() -> dict[str, str]:
    return {"GIT_TERMINAL_PROMPT": "0"}


def _push_environment(
    config: GitHub2LabConfig,
    remote: ValidatedGitRemote,
) -> dict[str, str]:
    environment = _base_git_environment()
    if remote.kind != "https":
        return environment

    assert remote.https_origin is not None
    scoped_credential_helper = f"credential.{remote.url}.helper"
    scoped_http_configuration = f"http.{remote.url}"
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "9",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": _HTTP_CREDENTIAL_HELPER,
            "GIT_CONFIG_KEY_2": scoped_credential_helper,
            "GIT_CONFIG_VALUE_2": "",
            "GIT_CONFIG_KEY_3": scoped_credential_helper,
            "GIT_CONFIG_VALUE_3": _HTTP_CREDENTIAL_HELPER,
            "GIT_CONFIG_KEY_4": f"{scoped_http_configuration}.followRedirects",
            "GIT_CONFIG_VALUE_4": "false",
            "GIT_CONFIG_KEY_5": f"{scoped_http_configuration}.sslVerify",
            "GIT_CONFIG_VALUE_5": "true",
            "GIT_CONFIG_KEY_6": "protocol.allow",
            "GIT_CONFIG_VALUE_6": "never",
            "GIT_CONFIG_KEY_7": "protocol.https.allow",
            "GIT_CONFIG_VALUE_7": "always",
            "GIT_CONFIG_KEY_8": "core.hooksPath",
            "GIT_CONFIG_VALUE_8": os.devnull,
            _GITLAB_TOKEN_ENVIRONMENT_VARIABLE: config.gitlab_token,
            _GITLAB_HOST_ENVIRONMENT_VARIABLE: remote.https_origin.credential_host,
            _GITLAB_ALTERNATE_HOST_ENVIRONMENT_VARIABLE: (
                remote.https_origin.credential_host_with_explicit_default_port
            ),
        }
    )
    return environment


def _is_unsafe_inherited_git_environment_variable(name: str) -> bool:
    return (
        name in _UNSAFE_GIT_ENVIRONMENT_VARIABLES
        or name.startswith("GIT_CONFIG_")
        or name.startswith("GIT_TRACE")
    )
