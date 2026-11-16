# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Unit tests for GitLab branch creation and Git push behavior."""

from __future__ import annotations

import os
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


class InterceptingPushExecutor:
    """Run local Git inspection commands while preventing a real push."""

    def __init__(self) -> None:
        self._executor = SubprocessGitCommandExecutor()
        self.commands: list[RecordedCommand] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
    ) -> GitCommandResult:
        command = RecordedCommand(tuple(arguments), cwd, dict(environment))
        self.commands.append(command)
        if len(arguments) > 1 and arguments[1] == "push":
            return _success()
        return self._executor.run(arguments, cwd=cwd, environment=environment)


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


def _https_push_environment(remote_url: str) -> dict[str, str]:
    executor = FakeGitCommandExecutor(
        [_success(GITHUB_COMMIT_SHA), _success(remote_url), _success()]
    )
    GitLabBranchPusher(
        _config(gitlab_remote_url=remote_url),
        executor=executor,
        repository_path="/workspace/source",
    ).push_current_commit()
    return dict(executor.commands[2].environment)


def _initialize_repository(path: Path) -> None:
    subprocess.run(("git", "init", "--quiet", str(path)), check=True)


def _initialize_committed_repository(path: Path) -> str:
    _initialize_repository(path)
    (path / "tracked.txt").write_text("test\n", encoding="utf-8")
    subprocess.run(("git", "add", "tracked.txt"), cwd=path, check=True)
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=ClusterHop Test",
            "-c",
            "user.email=clusterhop@example.test",
            "commit",
            "--quiet",
            "-m",
            "test commit",
        ),
        cwd=path,
        check=True,
    )
    return subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=path,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _base_environment(repository_path: Path) -> dict[str, str]:
    return {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(repository_path.resolve()),
    }


def test_subprocess_executor_removes_inherited_secrets_from_git_environment() -> None:
    executor = SubprocessGitCommandExecutor()
    completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")

    with (
        patch.dict(
            "os.environ",
            {
                "KEYLOOM_PASSWORD": "vault-password",
                "GITLAB_TOKEN": DUMMY_GITLAB_TOKEN,
                "GITLAB_TRIGGER_TOKEN": DUMMY_TRIGGER_TOKEN,
                "CLUSTERHOP_GITLAB_GIT_TOKEN": "stale-token",
                "GIT_CONFIG_PARAMETERS": "'http.followRedirects'='true'",
                "GIT_CONFIG_GLOBAL": "/tmp/untrusted-git-config",
                "GIT_SSL_NO_VERIFY": "true",
                "GIT_TRACE_CURL": "1",
                "GIT_TRACE_REDACT": "0",
                "GIT_EXEC_PATH": "/tmp/untrusted-git-exec-path",
                "GIT_ASKPASS": "/tmp/untrusted-git-askpass",
                "SSH_ASKPASS": "/tmp/untrusted-ssh-askpass",
            },
            clear=True,
        ),
        patch("subprocess.run", return_value=completed) as run,
    ):
        executor.run(("git", "status"), cwd=Path("/workspace"), environment={})

    process_environment = run.call_args.kwargs["env"]
    assert "KEYLOOM_PASSWORD" not in process_environment
    assert "GITLAB_TOKEN" not in process_environment
    assert "GITLAB_TRIGGER_TOKEN" not in process_environment
    assert "CLUSTERHOP_GITLAB_GIT_TOKEN" not in process_environment
    assert "GIT_CONFIG_PARAMETERS" not in process_environment
    assert "GIT_CONFIG_GLOBAL" not in process_environment
    assert "GIT_SSL_NO_VERIFY" not in process_environment
    assert "GIT_TRACE_CURL" not in process_environment
    assert "GIT_TRACE_REDACT" not in process_environment
    assert "GIT_EXEC_PATH" not in process_environment
    assert "GIT_ASKPASS" not in process_environment
    assert "SSH_ASKPASS" not in process_environment


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
    executor = FakeGitCommandExecutor(
        [
            _success(f"{GITHUB_COMMIT_SHA}\n"),
            _success(f"{GITLAB_REMOTE_URL}\n"),
            _success(),
        ]
    )
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
            environment=_base_environment(repository_path),
        ),
        RecordedCommand(
            arguments=("git", "ls-remote", "--get-url", "--", GITLAB_REMOTE_URL),
            cwd=repository_path,
            environment=_base_environment(repository_path),
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
            environment=_base_environment(repository_path),
        ),
    ]


def test_https_push_uses_ephemeral_environment_credentials() -> None:
    remote_url = "https://gitlab.example.test/group/project.git"
    executor = FakeGitCommandExecutor(
        [_success(GITHUB_COMMIT_SHA), _success(remote_url), _success()]
    )
    pusher = GitLabBranchPusher(
        _config(gitlab_remote_url=remote_url),
        executor=executor,
        repository_path="/workspace/source",
    )

    pusher.push_current_commit()

    push_command = executor.commands[2]
    assert push_command.arguments[4] == remote_url
    assert push_command.environment["GIT_TERMINAL_PROMPT"] == "0"
    assert push_command.environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert push_command.environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert push_command.environment["GIT_CONFIG_COUNT"] == "10"
    assert push_command.environment["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert push_command.environment["GIT_CONFIG_VALUE_0"] == ""
    assert push_command.environment["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert "CLUSTERHOP_GITLAB_GIT_TOKEN" in push_command.environment["GIT_CONFIG_VALUE_1"]
    assert "CLUSTERHOP_GITLAB_GIT_HOST" in push_command.environment["GIT_CONFIG_VALUE_1"]
    assert push_command.environment["GIT_CONFIG_KEY_2"] == f"credential.{remote_url}.helper"
    assert push_command.environment["GIT_CONFIG_VALUE_2"] == ""
    assert push_command.environment["GIT_CONFIG_KEY_3"] == f"credential.{remote_url}.helper"
    assert "CLUSTERHOP_GITLAB_GIT_TOKEN" in push_command.environment["GIT_CONFIG_VALUE_3"]
    assert push_command.environment["GIT_CONFIG_KEY_4"] == f"http.{remote_url}.followRedirects"
    assert push_command.environment["GIT_CONFIG_VALUE_4"] == "false"
    assert push_command.environment["GIT_CONFIG_KEY_5"] == f"http.{remote_url}.sslVerify"
    assert push_command.environment["GIT_CONFIG_VALUE_5"] == "true"
    assert push_command.environment["GIT_CONFIG_KEY_6"] == "protocol.allow"
    assert push_command.environment["GIT_CONFIG_VALUE_6"] == "never"
    assert push_command.environment["GIT_CONFIG_KEY_7"] == "protocol.https.allow"
    assert push_command.environment["GIT_CONFIG_VALUE_7"] == "always"
    assert push_command.environment["GIT_CONFIG_KEY_8"] == "core.hooksPath"
    assert push_command.environment["GIT_CONFIG_VALUE_8"] == os.devnull
    assert push_command.environment["GIT_CONFIG_KEY_9"] == "safe.directory"
    assert push_command.environment["GIT_CONFIG_VALUE_9"] == str(
        Path("/workspace/source").resolve()
    )
    assert push_command.environment["GIT_CONFIG_VALUE_9"] != "*"
    assert push_command.environment["CLUSTERHOP_GITLAB_GIT_TOKEN"] == DUMMY_GITLAB_TOKEN
    assert push_command.environment["CLUSTERHOP_GITLAB_GIT_HOST"] == "gitlab.example.test"
    assert all(DUMMY_GITLAB_TOKEN not in argument for argument in push_command.arguments)
    assert all(DUMMY_TRIGGER_TOKEN not in argument for argument in push_command.arguments)


def test_https_credential_helper_refuses_unintended_hosts() -> None:
    remote_url = "https://gitlab.example.test/group/project.git"
    helper_environment = {**os.environ, **_https_push_environment(remote_url)}

    trusted = subprocess.run(
        ("git", "credential", "fill"),
        input="protocol=https\nhost=gitlab.example.test\n\n",
        text=True,
        capture_output=True,
        check=False,
        env=helper_environment,
    )
    untrusted = subprocess.run(
        ("git", "credential", "fill"),
        input="protocol=https\nhost=other.example.test\n\n",
        text=True,
        capture_output=True,
        check=False,
        env=helper_environment,
    )

    assert trusted.returncode == 0
    assert f"password={DUMMY_GITLAB_TOKEN}" in trusted.stdout
    assert untrusted.returncode != 0
    assert DUMMY_GITLAB_TOKEN not in untrusted.stdout
    assert DUMMY_GITLAB_TOKEN not in untrusted.stderr


def test_real_git_security_settings_override_hostile_inherited_and_local_config(
    tmp_path: Path,
) -> None:
    remote_url = "https://gitlab.example.test/group/project.git"
    repository = tmp_path / "repository"
    repository.mkdir()
    _initialize_repository(repository)
    subprocess.run(
        (
            "git",
            "config",
            "--local",
            f"http.{remote_url}.followRedirects",
            "true",
        ),
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ("git", "config", "--local", f"http.{remote_url}.sslVerify", "false"),
        cwd=repository,
        check=True,
    )
    environment = _https_push_environment(remote_url)
    executor = SubprocessGitCommandExecutor()

    with patch.dict(
        "os.environ",
        {
            **os.environ,
            "GIT_CONFIG_PARAMETERS": (
                "'http.https://gitlab.example.test/group/project.git.followRedirects'='true'"
            ),
            "GIT_SSL_NO_VERIFY": "true",
        },
        clear=True,
    ):
        redirects = executor.run(
            ("git", "config", "--get-urlmatch", "http.followRedirects", remote_url),
            cwd=repository,
            environment=environment,
        )
        tls = executor.run(
            ("git", "config", "--get-urlmatch", "http.sslVerify", remote_url),
            cwd=repository,
            environment=environment,
        )

    assert redirects.stdout.strip() == "false"
    assert tls.stdout.strip() == "true"


def test_real_git_url_specific_credential_helper_is_neutralized(tmp_path: Path) -> None:
    remote_url = "https://gitlab.example.test/group/project.git"
    repository = tmp_path / "repository"
    repository.mkdir()
    _initialize_repository(repository)
    marker = tmp_path / "untrusted-helper-ran"
    malicious_helper = f"!f() {{ touch '{marker}'; }}; f"
    subprocess.run(
        ("git", "config", "--local", f"credential.{remote_url}.helper", malicious_helper),
        cwd=repository,
        check=True,
    )
    environment = {**os.environ, **_https_push_environment(remote_url)}

    credential = subprocess.run(
        ("git", "credential", "fill"),
        cwd=repository,
        env=environment,
        input=f"url={remote_url}\n\n",
        text=True,
        capture_output=True,
        check=False,
    )

    assert credential.returncode == 0
    assert f"password={DUMMY_GITLAB_TOKEN}" in credential.stdout
    assert not marker.exists()


def test_real_git_configured_pre_push_hook_is_disabled(tmp_path: Path) -> None:
    remote_url = "https://gitlab.example.test/group/project.git"
    repository = tmp_path / "repository"
    hooks = tmp_path / "hooks"
    repository.mkdir()
    hooks.mkdir()
    _initialize_repository(repository)
    marker = tmp_path / "pre-push-hook-ran"
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o700)
    subprocess.run(
        ("git", "config", "--local", "core.hooksPath", str(hooks)),
        cwd=repository,
        check=True,
    )
    executor = SubprocessGitCommandExecutor()

    result = executor.run(
        ("git", "hook", "run", "pre-push"),
        cwd=repository,
        environment=_https_push_environment(remote_url),
    )

    assert result.return_code != 0
    assert not marker.exists()


def test_real_git_rejects_simulated_different_owner_without_safe_directory(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _initialize_committed_repository(repository)
    executor = SubprocessGitCommandExecutor()

    with patch.dict(
        "os.environ",
        {**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"},
        clear=True,
    ):
        result = executor.run(
            ("git", "rev-parse", "--verify", "HEAD^{commit}"),
            cwd=repository,
            environment={
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            },
        )

    assert result.return_code != 0
    assert "dubious ownership" in result.stderr


def test_real_git_pusher_trusts_only_selected_repository_under_different_ownership(
    tmp_path: Path,
) -> None:
    remote_url = "https://gitlab.example.test/group/project.git"
    repository = tmp_path / "repository"
    repository.mkdir()
    commit_sha = _initialize_committed_repository(repository)
    executor = InterceptingPushExecutor()
    pusher = GitLabBranchPusher(
        _config(gitlab_remote_url=remote_url, github_commit_sha=commit_sha),
        executor=executor,
        repository_path=repository,
    )

    with patch.dict(
        "os.environ",
        {**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"},
        clear=True,
    ):
        branch_name = pusher.push_current_commit()

    assert branch_name == f"ci/{commit_sha}"
    assert len(executor.commands) == 3
    trusted_path = str(repository.resolve())
    for command in executor.commands:
        values = tuple(
            value
            for name, value in command.environment.items()
            if name.startswith("GIT_CONFIG_VALUE_")
        )
        assert trusted_path in values
        assert "*" not in values
    push_command = executor.commands[2]
    assert push_command.environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert push_command.environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert push_command.environment["GIT_CONFIG_KEY_9"] == "safe.directory"
    assert push_command.environment["GIT_CONFIG_VALUE_9"] == trusted_path
    assert all(DUMMY_GITLAB_TOKEN not in argument for argument in push_command.arguments)
    assert all(DUMMY_TRIGGER_TOKEN not in argument for argument in push_command.arguments)


def test_unexpected_git_url_rewrite_is_rejected_before_credentials_or_push() -> None:
    remote_url = "https://gitlab.example.test/group/project.git"
    rewritten_url = "https://other.example.test/group/project.git"
    executor = FakeGitCommandExecutor([_success(GITHUB_COMMIT_SHA), _success(rewritten_url)])
    pusher = GitLabBranchPusher(
        _config(gitlab_remote_url=remote_url),
        executor=executor,
    )

    with pytest.raises(GitPushError, match="rewrote"):
        pusher.push_current_commit()

    assert len(executor.commands) == 2
    assert all(
        "CLUSTERHOP_GITLAB_GIT_TOKEN" not in command.environment for command in executor.commands
    )


def test_real_git_url_rewrite_is_rejected_before_authenticated_push(tmp_path: Path) -> None:
    remote_url = "https://gitlab.example.test/group/project.git"
    repository = tmp_path / "repository"
    repository.mkdir()
    commit_sha = _initialize_committed_repository(repository)
    subprocess.run(
        (
            "git",
            "config",
            "--local",
            "url.https://other.example.test/.insteadOf",
            "https://gitlab.example.test/",
        ),
        cwd=repository,
        check=True,
    )
    pusher = GitLabBranchPusher(
        _config(gitlab_remote_url=remote_url, github_commit_sha=commit_sha),
        repository_path=repository,
    )

    with pytest.raises(GitPushError, match="rewrote") as captured:
        pusher.push_current_commit()

    assert DUMMY_GITLAB_TOKEN not in str(captured.value)
    assert DUMMY_TRIGGER_TOKEN not in str(captured.value)


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
            _success(GITLAB_REMOTE_URL),
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


@pytest.mark.parametrize("failure_on_command", [1, 2, 3])
def test_command_execution_failure_is_sanitized(failure_on_command: int) -> None:
    execution_error = GitCommandExecutionError(
        f"execution failed with {DUMMY_GITLAB_TOKEN} and {DUMMY_TRIGGER_TOKEN}"
    )
    outcomes: list[GitCommandResult | GitCommandExecutionError]
    if failure_on_command == 1:
        outcomes = [execution_error]
    elif failure_on_command == 2:
        outcomes = [_success(GITHUB_COMMIT_SHA), execution_error]
    else:
        outcomes = [_success(GITHUB_COMMIT_SHA), _success(GITLAB_REMOTE_URL), execution_error]
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
