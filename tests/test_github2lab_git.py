# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Unit tests for GitLab branch creation and Git push behavior."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

from clusterhop.github2lab.config import GitHub2LabConfig
from clusterhop.github2lab.git import (
    GitCommandExecutionError,
    GitCommandResult,
    GitLabBranchPusher,
    GitPushError,
    SubprocessGitCommandExecutor,
    create_ci_branch_name,
)

DUMMY_GITLAB_TOKEN = "dummy-private-token"
DUMMY_TRIGGER_TOKEN = "dummy-trigger-token"
GITHUB_COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"
GITLAB_REMOTE_URL = "git@gitlab.example.test:group/project.git"


@dataclass(frozen=True)
class RecordedCommand:
    """One command received by the fake executor."""

    arguments: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str] = field(repr=False)


class FakeGitCommandExecutor:
    """Queue-based Git executor that never invokes a real process."""

    def __init__(self, outcomes: Sequence[GitCommandResult | GitCommandExecutionError]) -> None:
        self._outcomes = list(outcomes)
        self.commands: list[RecordedCommand] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
    ) -> GitCommandResult:
        self.commands.append(
            RecordedCommand(
                arguments=tuple(arguments),
                cwd=cwd,
                environment=dict(environment),
            )
        )
        if not self._outcomes:
            raise AssertionError("The fake Git executor has no queued outcome.")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, GitCommandExecutionError):
            raise outcome
        return outcome


def _config(**overrides: object) -> GitHub2LabConfig:
    values: dict[str, object] = {
        "gitlab_token": DUMMY_GITLAB_TOKEN,
        "gitlab_trigger_token": DUMMY_TRIGGER_TOKEN,
        "gitlab_url": "https://gitlab.example.test",
        "gitlab_remote_url": GITLAB_REMOTE_URL,
        "gitlab_project_id": "1234",
        "gitlab_project_path": None,
        "github_commit_sha": GITHUB_COMMIT_SHA,
        "github_repository": "example/source-project",
        "github_run_id": "987654",
    }
    values.update(overrides)
    return GitHub2LabConfig(**values)  # type: ignore[arg-type]


def _success(stdout: str = "") -> GitCommandResult:
    return GitCommandResult(return_code=0, stdout=stdout, stderr="")


@pytest.mark.parametrize(
    "commit_sha",
    [
        GITHUB_COMMIT_SHA,
        GITHUB_COMMIT_SHA.upper(),
        "0123456789abcdef" * 4,
    ],
)
def test_create_ci_branch_name_uses_complete_commit_sha(commit_sha: str) -> None:
    assert create_ci_branch_name(commit_sha) == f"ci/{commit_sha}"


@pytest.mark.parametrize(
    "commit_sha",
    [
        "",
        "abc123",
        "g" * 40,
        f"{GITHUB_COMMIT_SHA}:malicious",
        f"{GITHUB_COMMIT_SHA}/extra",
        f"{GITHUB_COMMIT_SHA}\nmain",
    ],
)
def test_create_ci_branch_name_rejects_unsafe_commit_sha(commit_sha: str) -> None:
    with pytest.raises(GitPushError, match="40- or 64-character hexadecimal"):
        create_ci_branch_name(commit_sha)


def test_push_current_commit_uses_expected_refspec_and_ci_skip() -> None:
    repository_path = Path("/workspace/source")
    executor = FakeGitCommandExecutor([_success(f"{GITHUB_COMMIT_SHA}\n"), _success()])
    pusher = GitLabBranchPusher(
        _config(),
        executor=executor,
        repository_path=repository_path,
    )

    branch_name = pusher.push_current_commit()

    assert branch_name == f"ci/{GITHUB_COMMIT_SHA}"
    assert executor.commands == [
        RecordedCommand(
            arguments=("git", "rev-parse", "--verify", "HEAD^{commit}"),
            cwd=repository_path,
            environment={"GIT_TERMINAL_PROMPT": "0"},
        ),
        RecordedCommand(
            arguments=(
                "git",
                "push",
                "--push-option=ci.skip",
                "--",
                GITLAB_REMOTE_URL,
                f"HEAD:refs/heads/ci/{GITHUB_COMMIT_SHA}",
            ),
            cwd=repository_path,
            environment={"GIT_TERMINAL_PROMPT": "0"},
        ),
    ]


def test_https_push_uses_ephemeral_environment_credentials() -> None:
    remote_url = "https://gitlab.example.test/group/project.git"
    executor = FakeGitCommandExecutor([_success(GITHUB_COMMIT_SHA), _success()])
    pusher = GitLabBranchPusher(
        _config(gitlab_remote_url=remote_url),
        executor=executor,
        repository_path="/workspace/source",
    )

    pusher.push_current_commit()

    push_command = executor.commands[1]
    assert push_command.arguments[4] == remote_url
    assert push_command.environment["GIT_TERMINAL_PROMPT"] == "0"
    assert push_command.environment["GIT_CONFIG_COUNT"] == "2"
    assert push_command.environment["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert push_command.environment["GIT_CONFIG_VALUE_0"] == ""
    assert push_command.environment["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert "CLUSTERHOP_GITLAB_GIT_TOKEN" in push_command.environment["GIT_CONFIG_VALUE_1"]
    assert push_command.environment["CLUSTERHOP_GITLAB_GIT_TOKEN"] == DUMMY_GITLAB_TOKEN
    assert all(DUMMY_GITLAB_TOKEN not in argument for argument in push_command.arguments)
    assert all(DUMMY_TRIGGER_TOKEN not in argument for argument in push_command.arguments)


def test_pusher_repr_does_not_expose_configuration() -> None:
    pusher = GitLabBranchPusher(
        _config(),
        executor=FakeGitCommandExecutor([]),
        repository_path="/workspace/source",
    )

    representation = repr(pusher)

    assert representation == "GitLabBranchPusher()"
    assert DUMMY_GITLAB_TOKEN not in representation
    assert DUMMY_TRIGGER_TOKEN not in representation
    assert GITLAB_REMOTE_URL not in representation


def test_invalid_configured_sha_fails_before_running_git() -> None:
    executor = FakeGitCommandExecutor([])
    pusher = GitLabBranchPusher(
        _config(github_commit_sha="not-a-commit"),
        executor=executor,
    )

    with pytest.raises(GitPushError, match="github_commit_sha"):
        pusher.push_current_commit()

    assert executor.commands == []


def test_mismatched_head_is_rejected_without_push() -> None:
    different_commit = "f" * 40
    executor = FakeGitCommandExecutor([_success(different_commit)])
    pusher = GitLabBranchPusher(_config(), executor=executor)

    with pytest.raises(GitPushError, match="does not match"):
        pusher.push_current_commit()

    assert len(executor.commands) == 1


@pytest.mark.parametrize("head_output", ["", "HEAD", "abc123", "z" * 40])
def test_invalid_head_output_is_rejected_without_push(head_output: str) -> None:
    executor = FakeGitCommandExecutor([_success(head_output)])
    pusher = GitLabBranchPusher(_config(), executor=executor)

    with pytest.raises(GitPushError, match="invalid commit identifier"):
        pusher.push_current_commit()

    assert len(executor.commands) == 1


def test_head_lookup_failure_is_sanitized() -> None:
    sensitive_output = f"failure {DUMMY_GITLAB_TOKEN} {DUMMY_TRIGGER_TOKEN}"
    executor = FakeGitCommandExecutor(
        [GitCommandResult(return_code=128, stdout="", stderr=sensitive_output)]
    )
    pusher = GitLabBranchPusher(_config(), executor=executor)

    with pytest.raises(GitPushError) as captured:
        pusher.push_current_commit()

    message = str(captured.value)
    assert message == "Unable to verify the current Git commit: Git exited with status 128."
    assert DUMMY_GITLAB_TOKEN not in message
    assert DUMMY_TRIGGER_TOKEN not in message
    assert sensitive_output not in message


def test_push_failure_is_sanitized() -> None:
    sensitive_output = f"remote rejected {DUMMY_GITLAB_TOKEN} {DUMMY_TRIGGER_TOKEN}"
    executor = FakeGitCommandExecutor(
        [
            _success(GITHUB_COMMIT_SHA),
            GitCommandResult(return_code=1, stdout="", stderr=sensitive_output),
        ]
    )
    pusher = GitLabBranchPusher(_config(), executor=executor)

    with pytest.raises(GitPushError) as captured:
        pusher.push_current_commit()

    message = str(captured.value)
    assert message == "Unable to push the GitLab CI branch: Git exited with status 1."
    assert DUMMY_GITLAB_TOKEN not in message
    assert DUMMY_TRIGGER_TOKEN not in message
    assert sensitive_output not in message


@pytest.mark.parametrize("failure_on_command", [1, 2])
def test_command_execution_failure_is_sanitized(failure_on_command: int) -> None:
    execution_error = GitCommandExecutionError(
        f"execution failed with {DUMMY_GITLAB_TOKEN} and {DUMMY_TRIGGER_TOKEN}"
    )
    outcomes: list[GitCommandResult | GitCommandExecutionError]
    if failure_on_command == 1:
        outcomes = [execution_error]
    else:
        outcomes = [_success(GITHUB_COMMIT_SHA), execution_error]
    pusher = GitLabBranchPusher(_config(), executor=FakeGitCommandExecutor(outcomes))

    with pytest.raises(GitPushError) as captured:
        pusher.push_current_commit()

    message = str(captured.value)
    assert DUMMY_GITLAB_TOKEN not in message
    assert DUMMY_TRIGGER_TOKEN not in message


def test_git_command_result_repr_hides_captured_output() -> None:
    result = GitCommandResult(
        return_code=1,
        stdout=DUMMY_GITLAB_TOKEN,
        stderr=DUMMY_TRIGGER_TOKEN,
    )

    representation = repr(result)

    assert representation == "GitCommandResult(return_code=1)"
    assert DUMMY_GITLAB_TOKEN not in representation
    assert DUMMY_TRIGGER_TOKEN not in representation


def test_subprocess_executor_uses_no_shell_and_captures_output() -> None:
    executor = SubprocessGitCommandExecutor(timeout_seconds=15)
    completed = subprocess.CompletedProcess(
        args=("git", "status"),
        returncode=0,
        stdout="clean\n",
        stderr="",
    )

    with patch("clusterhop.github2lab.git.subprocess.run", return_value=completed) as run:
        result = executor.run(
            ("git", "status"),
            cwd=Path("/workspace/source"),
            environment={"GIT_TERMINAL_PROMPT": "0"},
        )

    assert result == GitCommandResult(return_code=0, stdout="clean\n", stderr="")
    positional_arguments, keyword_arguments = run.call_args
    assert positional_arguments == (("git", "status"),)
    assert keyword_arguments["cwd"] == Path("/workspace/source")
    assert keyword_arguments["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert keyword_arguments["stdin"] is subprocess.DEVNULL
    assert keyword_arguments["check"] is False
    assert keyword_arguments["timeout"] == 15.0
    assert "shell" not in keyword_arguments


@pytest.mark.parametrize(
    "execution_failure",
    [
        OSError("dummy operating-system failure"),
        ValueError("dummy invalid process argument"),
        subprocess.TimeoutExpired(cmd=("git", "status"), timeout=1),
    ],
)
def test_subprocess_executor_sanitizes_process_failures(execution_failure: Exception) -> None:
    executor = SubprocessGitCommandExecutor()

    with patch("clusterhop.github2lab.git.subprocess.run", side_effect=execution_failure):
        with pytest.raises(GitCommandExecutionError) as captured:
            executor.run(
                ("git", "status"),
                cwd=Path("/workspace/source"),
                environment={},
            )

    assert str(captured.value) == "Git command execution failed."


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_subprocess_executor_rejects_invalid_timeout(timeout: object) -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        SubprocessGitCommandExecutor(timeout_seconds=timeout)  # type: ignore[arg-type]


def test_subprocess_executor_repr_contains_only_timeout() -> None:
    executor = SubprocessGitCommandExecutor(timeout_seconds=15)

    assert repr(executor) == "SubprocessGitCommandExecutor(timeout_seconds=15.0)"
