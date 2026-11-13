# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Tests for the reusable GitHub Action lifecycle adapter."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import pytest
import yaml
from keyloom import encrypt_value  # type: ignore[import-untyped]

from clusterhop.github2lab.models import GitLabPipeline
from clusterhop.github2lab.state import FilePipelineStateStore, PipelineState
from clusterhop.github_action import (
    GitHubActionError,
    action_paths,
    cleanup_action_run,
    main,
    prepare_action_run,
)

DUMMY_TOKEN = "dummy-action-token-that-must-not-appear"
DUMMY_TRIGGER_TOKEN = "dummy-action-trigger-token-that-must-not-appear"
DUMMY_PASSWORD = "dummy-action-password-that-must-not-appear"


@lru_cache(maxsize=2)
def _encrypted_value(value: str) -> str:
    return cast(
        str,
        encrypt_value(value=value, password=DUMMY_PASSWORD, algorithm="argon2-v2"),
    )


def _environment(tmp_path: Path) -> dict[str, str]:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    return {
        "GITHUB_WORKSPACE": str(workspace),
        "HOME": str(home),
        "GITHUB_RUN_ID": "123456",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "send-to-gitlab",
        "GITHUB_ACTION": "clusterhop",
        "GITHUB_SHA": "0123456789abcdef0123456789abcdef01234567",
        "GITHUB_REPOSITORY": "example/source",
        "KEYLOOM_PASSWORD": DUMMY_PASSWORD,
    }


def _write_secrets(
    environ: dict[str, str], relative: str = ".github/clusterhop/vault.yaml"
) -> Path:
    path = Path(environ["GITHUB_WORKSPACE"]) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "gitlab_token:\n"
        "  __keyloom__: secret\n"
        f'  value: "{_encrypted_value(DUMMY_TOKEN)}"\n'
        "gitlab_trigger_token:\n"
        "  __keyloom__: secret\n"
        f'  value: "{_encrypted_value(DUMMY_TRIGGER_TOKEN)}"\n',
        encoding="utf-8",
    )
    return path


def _prepare(
    environ: dict[str, str], *, machine: str = "jureca", secrets: str = "vault.yaml"
) -> tuple[str, ...]:
    _write_secrets(environ, secrets)
    return prepare_action_run(
        gitlab_url="https://gitlab.example.test",
        gitlab_project="group/nested/project",
        jsc_machine=machine,
        secrets_file=secrets,
        environ=environ,
    )


def test_action_metadata_declares_approved_contract() -> None:
    metadata = yaml.safe_load(Path("action.yml").read_text(encoding="utf-8"))

    assert metadata["inputs"] == {
        "gitlab-url": {
            "description": "Base URL of the target GitLab instance.",
            "required": True,
        },
        "gitlab-project": {
            "description": "GitLab project path_with_namespace.",
            "required": True,
        },
        "jsc-machine": {
            "description": (
                "Optional JSC machine name used to generate the ClusterHop pipeline variable."
            ),
            "required": False,
            "default": "",
        },
        "secrets-file": {
            "description": "Relative path to the Keyloom-encrypted GitLab credentials file.",
            "required": True,
        },
    }
    assert metadata["runs"]["using"] == "docker"
    assert metadata["runs"]["image"] == "Dockerfile"
    assert metadata["runs"]["post-entrypoint"].endswith("clusterhop-action-cleanup")


def test_prepare_action_run_writes_non_secret_inputs_and_cli_command(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    secrets = ".github/clusterhop/custom-name.keyloom.yaml"

    command = _prepare(environ, secrets=secrets)
    paths = action_paths(environ)
    config = json.loads(paths.config.read_text(encoding="utf-8"))
    options = json.loads(paths.options.read_text(encoding="utf-8"))
    metadata = json.loads(paths.metadata.read_text(encoding="utf-8"))

    assert config == {
        "destination_type": "gitlab",
        "gitlab_project_path": "group/nested/project",
        "gitlab_url": "https://gitlab.example.test",
        "jsc_machine": "jureca",
    }
    assert options == {"pipeline_state_file": str(paths.pipeline_state)}
    assert command == (
        "clusterhop",
        "run",
        "--config",
        str(paths.config),
        "--options",
        str(paths.options),
        str(Path(environ["GITHUB_WORKSPACE"]).resolve()),
        "--secrets",
        str((Path(environ["GITHUB_WORKSPACE"]) / secrets).resolve()),
    )
    serialized = json.dumps({"config": config, "options": options, "metadata": metadata})
    assert DUMMY_TOKEN not in serialized
    assert DUMMY_TRIGGER_TOKEN not in serialized
    assert DUMMY_PASSWORD not in serialized
    assert paths.config.stat().st_mode & 0o777 == 0o600
    assert paths.options.stat().st_mode & 0o777 == 0o600
    assert paths.metadata.stat().st_mode & 0o777 == 0o600


def test_omitted_machine_writes_no_machine_selection(tmp_path: Path) -> None:
    environ = _environment(tmp_path)

    _prepare(environ, machine="")

    config = json.loads(action_paths(environ).config.read_text(encoding="utf-8"))
    assert "jsc_machine" not in config


@pytest.mark.parametrize("machine", ["jupiter", "JURECA", "future_machine_2"])
def test_arbitrary_valid_machine_is_preserved(tmp_path: Path, machine: str) -> None:
    environ = _environment(tmp_path)

    _prepare(environ, machine=machine)

    config = json.loads(action_paths(environ).config.read_text(encoding="utf-8"))
    assert config["jsc_machine"] == machine


@pytest.mark.parametrize("machine", ["invalid-machine", "2jureca", "machine name", " jureca"])
def test_invalid_machine_is_rejected_before_cli_execution(tmp_path: Path, machine: str) -> None:
    environ = _environment(tmp_path)
    _write_secrets(environ, "vault.yaml")

    with pytest.raises(GitHubActionError, match="jsc_machine"):
        prepare_action_run(
            gitlab_url="https://gitlab.example.test",
            gitlab_project="group/project",
            jsc_machine=machine,
            secrets_file="vault.yaml",
            environ=environ,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"gitlab_url": ""}, "gitlab-url is required"),
        ({"gitlab_project": ""}, "gitlab-project is required"),
        ({"secrets_file": ""}, "secrets-file is required"),
        ({"gitlab_url": "file:///tmp/gitlab"}, "gitlab-url is invalid"),
        ({"gitlab_project": "../project"}, "gitlab-project is invalid"),
    ],
)
def test_invalid_required_inputs_are_clear(
    tmp_path: Path, overrides: dict[str, str], message: str
) -> None:
    environ = _environment(tmp_path)
    _write_secrets(environ, "vault.yaml")
    values = {
        "gitlab_url": "https://gitlab.example.test",
        "gitlab_project": "group/project",
        "jsc_machine": "",
        "secrets_file": "vault.yaml",
    }
    values.update(overrides)

    with pytest.raises(GitHubActionError, match=message):
        prepare_action_run(environ=environ, **values)


@pytest.mark.parametrize("secrets_file", ["/tmp/secrets.yaml", "../secrets.yaml"])
def test_secrets_file_must_stay_inside_repository(tmp_path: Path, secrets_file: str) -> None:
    environ = _environment(tmp_path)

    with pytest.raises(GitHubActionError, match="inside the repository|relative"):
        prepare_action_run(
            gitlab_url="https://gitlab.example.test",
            gitlab_project="group/project",
            jsc_machine="",
            secrets_file=secrets_file,
            environ=environ,
        )


def test_nonexistent_relative_secrets_file_is_rejected(tmp_path: Path) -> None:
    environ = _environment(tmp_path)

    with pytest.raises(
        GitHubActionError,
        match="Action input secrets-file must identify a file inside the repository",
    ) as captured:
        prepare_action_run(
            gitlab_url="https://gitlab.example.test",
            gitlab_project="group/project",
            jsc_machine="",
            secrets_file=".github/clusterhop/missing-secrets.yaml",
            environ=environ,
        )

    assert "missing-secrets.yaml" not in str(captured.value)


def test_malformed_keyloom_yaml_is_rejected_with_sanitized_error(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    path = Path(environ["GITHUB_WORKSPACE"]) / "malformed-secrets.yaml"
    sensitive_content = "dummy-malformed-secret-that-must-not-appear"
    path.write_text(
        f'gitlab_token: ["{sensitive_content}"\n',
        encoding="utf-8",
    )

    with pytest.raises(
        GitHubActionError,
        match="Unable to validate the Keyloom secrets file",
    ) as captured:
        prepare_action_run(
            gitlab_url="https://gitlab.example.test",
            gitlab_project="group/project",
            jsc_machine="",
            secrets_file=path.name,
            environ=environ,
        )

    assert sensitive_content not in str(captured.value)


def test_missing_required_encrypted_credential_is_rejected(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    path = Path(environ["GITHUB_WORKSPACE"]) / "missing-credential.yaml"
    path.write_text(
        "gitlab_token:\n"
        "  __keyloom__: secret\n"
        f'  value: "{_encrypted_value(DUMMY_TOKEN)}"\n',
        encoding="utf-8",
    )

    with pytest.raises(
        GitHubActionError,
        match="requires a Keyloom-encrypted secrets file",
    ):
        prepare_action_run(
            gitlab_url="https://gitlab.example.test",
            gitlab_project="group/project",
            jsc_machine="",
            secrets_file=path.name,
            environ=environ,
        )


def test_action_rejects_plaintext_secrets_without_changing_cli_compatibility(
    tmp_path: Path,
) -> None:
    environ = _environment(tmp_path)
    path = Path(environ["GITHUB_WORKSPACE"]) / "plain-secrets.yaml"
    path.write_text(
        f"gitlab_token: {DUMMY_TOKEN}\n" f"gitlab_trigger_token: {DUMMY_TRIGGER_TOKEN}\n",
        encoding="utf-8",
    )

    with pytest.raises(GitHubActionError, match="requires a Keyloom-encrypted") as captured:
        prepare_action_run(
            gitlab_url="https://gitlab.example.test",
            gitlab_project="group/project",
            jsc_machine="",
            secrets_file="plain-secrets.yaml",
            environ=environ,
        )

    assert DUMMY_TOKEN not in str(captured.value)
    assert DUMMY_TRIGGER_TOKEN not in str(captured.value)


@pytest.mark.parametrize("encrypted_field", [None, "gitlab_token", "gitlab_trigger_token"])
def test_action_rejects_plaintext_or_partially_encrypted_tokens(
    tmp_path: Path,
    encrypted_field: str | None,
) -> None:
    environ = _environment(tmp_path)
    path = Path(environ["GITHUB_WORKSPACE"]) / "mixed-secrets.yaml"
    values = {
        "gitlab_token": DUMMY_TOKEN,
        "gitlab_trigger_token": DUMMY_TRIGGER_TOKEN,
    }
    lines: list[str] = []
    for field, value in values.items():
        if field == encrypted_field:
            lines.extend(
                (field + ":", "  __keyloom__: secret", f'  value: "{_encrypted_value(value)}"')
            )
        else:
            lines.append(f"{field}: {value}")
    lines.extend(("unrelated:", "  __keyloom__: secret", '  value: "$KEYLOOM;invalid"'))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(GitHubActionError, match="requires a Keyloom-encrypted"):
        prepare_action_run(
            gitlab_url="https://gitlab.example.test",
            gitlab_project="group/project",
            jsc_machine="",
            secrets_file=path.name,
            environ=environ,
        )


@pytest.mark.parametrize(
    "wrapper",
    [
        {"__keyloom__": "secret", "value": "__keyloom__secret__"},
        {"__keyloom__": "unsupported", "value": "$KEYLOOM;broken"},
        {"__keyloom__": "secret"},
        {"__keyloom__": "secret", "value": "$KEYLOOM;broken", "extra": "invalid"},
    ],
)
def test_action_rejects_placeholder_or_malformed_keyloom_wrappers(
    tmp_path: Path,
    wrapper: dict[str, str],
) -> None:
    environ = _environment(tmp_path)
    path = Path(environ["GITHUB_WORKSPACE"]) / "invalid-secrets.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "gitlab_token": wrapper,
                "gitlab_trigger_token": {
                    "__keyloom__": "secret",
                    "value": _encrypted_value(DUMMY_TRIGGER_TOKEN),
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(GitHubActionError, match="Keyloom"):
        prepare_action_run(
            gitlab_url="https://gitlab.example.test",
            gitlab_project="group/project",
            jsc_machine="",
            secrets_file=path.name,
            environ=environ,
        )


def test_action_structural_preflight_does_not_decrypt_ciphertext(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    path = Path(environ["GITHUB_WORKSPACE"]) / "structural-secrets.yaml"
    path.write_text(
        "gitlab_token:\n"
        "  __keyloom__: secret\n"
        '  value: "$KEYLOOM;malformed-but-structurally-encrypted"\n'
        "gitlab_trigger_token:\n"
        "  __keyloom__: secret\n"
        '  value: "$KEYLOOM;also-structurally-encrypted"\n',
        encoding="utf-8",
    )

    with patch("clusterhop.github_action._load_secrets_mapping") as decrypt:
        command = prepare_action_run(
            gitlab_url="https://gitlab.example.test",
            gitlab_project="group/project",
            jsc_machine="",
            secrets_file=path.name,
            environ=environ,
        )

    decrypt.assert_not_called()
    assert command[0:2] == ("clusterhop", "run")


def test_main_execs_cli_so_result_exit_code_and_signals_are_preserved(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    _write_secrets(environ, "vault.yaml")

    with patch.dict(os.environ, environ, clear=True), patch("os.execvp") as execute:
        with pytest.raises(AssertionError, match="returned unexpectedly"):
            main(
                (
                    "main",
                    "https://gitlab.example.test",
                    "group/project",
                    "",
                    "vault.yaml",
                )
            )

    command = execute.call_args.args[1]
    assert command[0:2] == ("clusterhop", "run")
    execute.assert_called_once()


def test_main_sanitizes_cli_launcher_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _environment(tmp_path)
    _write_secrets(environ, "vault.yaml")

    with (
        patch.dict(os.environ, environ, clear=True),
        patch("os.execvp", side_effect=OSError(f"unsafe {DUMMY_TOKEN}")),
    ):
        exit_code = main(("main", "https://gitlab.example.test", "group/project", "", "vault.yaml"))

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Unable to launch the ClusterHop CLI" in captured.err
    assert DUMMY_TOKEN not in captured.out
    assert DUMMY_TOKEN not in captured.err


def _write_active_state(environ: dict[str, str]) -> None:
    paths = action_paths(environ)
    FilePipelineStateStore(paths.pipeline_state).write(
        PipelineState(
            active=True,
            pipeline_id=42,
            pipeline_status="running",
            gitlab_ref="ci/0123456789abcdef0123456789abcdef01234567",
            project_identifier="id:8894",
            github_run_id="123456",
        )
    )


def test_post_cleanup_uses_existing_fallback_and_is_idempotent(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    _prepare(environ)
    _write_active_state(environ)
    fallback = Mock(return_value=SimpleNamespace(action="cancellation_accepted"))
    resolved_project = SimpleNamespace(
        id=8894,
        path_with_namespace="group/nested/project",
        http_url_to_repo="https://gitlab.example.test/group/nested/project.git",
    )

    with (
        patch(
            "clusterhop.api.resolve_gitlab_project", return_value=resolved_project
        ) as resolve_project,
        patch(
            "clusterhop.github_action._load_secrets_mapping",
            return_value={
                "gitlab_token": DUMMY_TOKEN,
                "gitlab_trigger_token": DUMMY_TRIGGER_TOKEN,
            },
        ) as load_secrets,
        patch("clusterhop.github_action.cancel_pipeline_from_state_environment", fallback),
    ):
        first = cleanup_action_run(environ)
        second = cleanup_action_run(environ)

    assert first == "cancellation_accepted"
    assert second == "no_state"
    fallback.assert_called_once()
    cleanup_environment = fallback.call_args.args[0]
    assert cleanup_environment["GITLAB_TOKEN"] == DUMMY_TOKEN
    assert cleanup_environment["GITLAB_TRIGGER_TOKEN"] == DUMMY_TRIGGER_TOKEN
    assert cleanup_environment["GITLAB_PROJECT_ID"] == "8894"
    assert cleanup_environment["GITLAB_REMOTE_URL"].endswith("group/nested/project.git")
    assert resolve_project.call_args.kwargs["request_timeout_seconds"] == 10.0
    load_secrets.assert_called_once()
    assert not action_paths(environ).directory.exists()


def test_inactive_post_cleanup_needs_no_keyloom_or_api(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    _prepare(environ)
    paths = action_paths(environ)
    FilePipelineStateStore(paths.pipeline_state).write(
        PipelineState(
            active=False,
            pipeline_id=42,
            pipeline_status="success",
            gitlab_ref="ci/0123456789abcdef0123456789abcdef01234567",
            project_identifier="id:8894",
            github_run_id="123456",
        )
    )

    with (
        patch("clusterhop.github_action._load_secrets_mapping") as load_secrets,
        patch("clusterhop.github_action.cancel_pipeline_from_state_environment") as fallback,
    ):
        result = cleanup_action_run(environ)

    assert result == "already_inactive"
    load_secrets.assert_not_called()
    fallback.assert_not_called()


def test_post_cleanup_keyloom_failure_is_secret_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _environment(tmp_path)
    _prepare(environ)
    _write_active_state(environ)

    with (
        patch.dict(os.environ, {**environ, "KEYLOOM_PASSWORD": "wrong-password"}, clear=True),
        patch(
            "clusterhop.github_action._load_secrets_mapping",
            side_effect=Exception(f"unsafe {DUMMY_TOKEN} {DUMMY_PASSWORD}"),
        ),
    ):
        exit_code = main(("cleanup",))

    captured = capsys.readouterr()
    assert exit_code == 2
    assert DUMMY_TOKEN not in captured.out
    assert DUMMY_TOKEN not in captured.err
    assert DUMMY_PASSWORD not in captured.out
    assert DUMMY_PASSWORD not in captured.err


def test_missing_post_state_is_successful_no_op(tmp_path: Path) -> None:
    environ = _environment(tmp_path)

    assert cleanup_action_run(environ) == "no_state"
