# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Tests for the ClusterHop command-line interface."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from keyloom import KeyloomError, encrypt_value  # type: ignore[import-untyped]

from clusterhop import RunConfigurationError, RunResult
from clusterhop.cli import main

DUMMY_TOKEN = "dummy-private-token-that-must-not-appear"
DUMMY_TRIGGER_TOKEN = "dummy-trigger-token-that-must-not-appear"
DUMMY_NESTED_SECRET = "dummy-nested-secret-that-must-not-appear"
DUMMY_VAULT_PASSWORD = "dummy-vault-password-that-must-not-appear"
DUMMY_PIPELINE_VALUE = "dummy-pipeline-secret-that-must-not-appear"


def _write_yaml_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    config_path = tmp_path / "config.yaml"
    options_path = tmp_path / "options.yaml"
    secrets_path = tmp_path / "secrets.yaml"
    config_path.write_text(
        "destination_type: gitlab\n"
        "gitlab_url: https://gitlab.example.test\n"
        "gitlab_remote_url: https://gitlab.example.test/group/project.git\n"
        "gitlab_project_id: 8894\n"
        "github_repository: example/source-project\n"
        "github_commit_sha: 0123456789abcdef0123456789abcdef01234567\n"
        'github_run_id: "987654"\n'
        "jsc_machine: jupiter\n"
        "extra_pipeline_variables:\n"
        "  CUSTOM_MODE: validation\n"
        '  EMPTY_VALUE: ""\n',
        encoding="utf-8",
    )
    options_path.write_text(
        "poll_interval_seconds: 3\n"
        "pipeline_timeout_seconds: 150\n"
        "pipeline_state_file: /tmp/github2lab-pipeline-state.json\n"
        "cleanup_timeout_seconds: 10\n",
        encoding="utf-8",
    )
    secrets_path.write_text(
        f"gitlab_token: {DUMMY_TOKEN}\ngitlab_trigger_token: dummy-trigger-token\n",
        encoding="utf-8",
    )
    return config_path, options_path, secrets_path


def _successful_result() -> RunResult:
    return RunResult(status="success", exit_code=0, summary="summary")


@pytest.fixture(scope="module")
def keyloom_envelopes() -> dict[str, str]:
    return {
        "gitlab_token": encrypt_value(
            value=DUMMY_TOKEN,
            password=DUMMY_VAULT_PASSWORD,
            algorithm="argon2-v2",
        ),
        "gitlab_trigger_token": encrypt_value(
            value=DUMMY_TRIGGER_TOKEN,
            password=DUMMY_VAULT_PASSWORD,
            algorithm="argon2-v2",
        ),
        "nested": encrypt_value(
            value=DUMMY_NESTED_SECRET,
            password=DUMMY_VAULT_PASSWORD,
            algorithm="argon2-v2",
        ),
    }


def _write_keyloom_secrets(path: Path, envelopes: dict[str, str], *, nested: bool = False) -> None:
    nested_content = (
        "metadata:\n"
        "  nested:\n"
        "    __keyloom__: secret\n"
        f'    value: "{envelopes["nested"]}"\n'
        if nested
        else ""
    )
    path.write_text(
        "gitlab_token:\n"
        "  __keyloom__: secret\n"
        f'  value: "{envelopes["gitlab_token"]}"\n'
        "gitlab_trigger_token:\n"
        "  __keyloom__: secret\n"
        f'  value: "{envelopes["gitlab_trigger_token"]}"\n'
        f"{nested_content}",
        encoding="utf-8",
    )


def test_new_run_command_loads_yaml_and_calls_public_api(tmp_path: Path) -> None:
    config_path, options_path, secrets_path = _write_yaml_inputs(tmp_path)
    backend = Mock(return_value=_successful_result())
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        "--options",
        str(options_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with patch.object(sys, "argv", arguments), patch("clusterhop.cli.clusterhop_run", backend):
        with pytest.raises(SystemExit, match="0"):
            main()

    backend.assert_called_once_with(
        config={
            "destination_type": "gitlab",
            "gitlab_url": "https://gitlab.example.test",
            "gitlab_remote_url": "https://gitlab.example.test/group/project.git",
            "gitlab_project_id": 8894,
            "github_repository": "example/source-project",
            "github_commit_sha": "0123456789abcdef0123456789abcdef01234567",
            "github_run_id": "987654",
            "jsc_machine": "jupiter",
            "extra_pipeline_variables": {
                "CUSTOM_MODE": "validation",
                "EMPTY_VALUE": "",
            },
        },
        repo_path=tmp_path,
        options={
            "poll_interval_seconds": 3,
            "pipeline_timeout_seconds": 150,
            "pipeline_state_file": "/tmp/github2lab-pipeline-state.json",
            "cleanup_timeout_seconds": 10,
        },
        secrets={
            "gitlab_token": DUMMY_TOKEN,
            "gitlab_trigger_token": "dummy-trigger-token",
        },
    )


def test_new_run_command_reports_invalid_pipeline_variables_safely(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    config_text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        config_text.replace("CUSTOM_MODE: validation", f"INVALID-NAME: {DUMMY_PIPELINE_VALUE}"),
        encoding="utf-8",
    )
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with patch.object(sys, "argv", arguments):
        with pytest.raises(SystemExit, match="2"):
            main()

    captured = capsys.readouterr()
    assert "extra_pipeline_variables" in captured.err
    assert DUMMY_PIPELINE_VALUE not in captured.out
    assert DUMMY_PIPELINE_VALUE not in captured.err


def test_new_run_command_reports_invalid_machine_safely(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    config_text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        config_text.replace("jsc_machine: jupiter", "jsc_machine: invalid-machine"),
        encoding="utf-8",
    )
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with patch.object(sys, "argv", arguments):
        with pytest.raises(SystemExit, match="2"):
            main()

    captured = capsys.readouterr()
    assert "jsc_machine" in captured.err
    assert DUMMY_TOKEN not in captured.out
    assert DUMMY_TOKEN not in captured.err


def test_new_run_command_passes_none_when_options_are_omitted(tmp_path: Path) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    backend = Mock(return_value=_successful_result())
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with patch.object(sys, "argv", arguments), patch("clusterhop.cli.clusterhop_run", backend):
        with pytest.raises(SystemExit, match="0"):
            main()

    assert backend.call_args.kwargs["options"] is None


def test_plain_secrets_do_not_require_keyloom_password(tmp_path: Path) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    backend = Mock(return_value=_successful_result())
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with (
        patch.dict("os.environ", {}, clear=True),
        patch.object(sys, "argv", arguments),
        patch("clusterhop.cli.keyloom_load") as load,
        patch("clusterhop.cli.clusterhop_run", backend),
    ):
        with pytest.raises(SystemExit, match="0"):
            main()

    load.assert_not_called()
    assert backend.call_args.kwargs["secrets"]["gitlab_token"] == DUMMY_TOKEN


def test_keyloom_secrets_are_decrypted_and_passed_to_public_api(
    tmp_path: Path,
    keyloom_envelopes: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    _write_keyloom_secrets(secrets_path, keyloom_envelopes, nested=True)
    backend = Mock(return_value=_successful_result())
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with (
        patch.dict("os.environ", {"KEYLOOM_PASSWORD": DUMMY_VAULT_PASSWORD}, clear=True),
        patch.object(sys, "argv", arguments),
        patch("clusterhop.cli.clusterhop_run", backend),
    ):
        with pytest.raises(SystemExit, match="0"):
            main()

    assert backend.call_args.kwargs["secrets"] == {
        "gitlab_token": DUMMY_TOKEN,
        "gitlab_trigger_token": DUMMY_TRIGGER_TOKEN,
        "metadata": {"nested": DUMMY_NESTED_SECRET},
    }
    encrypted_file = secrets_path.read_text(encoding="utf-8")
    assert "$KEYLOOM;" in encrypted_file
    assert DUMMY_TOKEN not in encrypted_file
    assert DUMMY_TRIGGER_TOKEN not in encrypted_file
    captured = capsys.readouterr()
    for sensitive_value in (DUMMY_TOKEN, DUMMY_TRIGGER_TOKEN, DUMMY_VAULT_PASSWORD):
        assert sensitive_value not in captured.out
        assert sensitive_value not in captured.err


@pytest.mark.parametrize("environment", [{}, {"KEYLOOM_PASSWORD": ""}])
def test_keyloom_secrets_require_non_empty_environment_password(
    tmp_path: Path,
    keyloom_envelopes: dict[str, str],
    environment: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    _write_keyloom_secrets(secrets_path, keyloom_envelopes)
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with patch.dict("os.environ", environment, clear=True), patch.object(sys, "argv", arguments):
        with pytest.raises(SystemExit, match="2"):
            main()

    captured = capsys.readouterr()
    assert "KEYLOOM_PASSWORD" in captured.err
    assert DUMMY_TOKEN not in captured.err
    assert DUMMY_TOKEN not in captured.out


def test_keyloom_wrong_password_is_secret_safe(
    tmp_path: Path,
    keyloom_envelopes: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    _write_keyloom_secrets(secrets_path, keyloom_envelopes)
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with (
        patch.dict("os.environ", {"KEYLOOM_PASSWORD": "wrong-password"}, clear=True),
        patch.object(sys, "argv", arguments),
    ):
        with pytest.raises(SystemExit, match="2"):
            main()

    captured = capsys.readouterr()
    assert "Unable to decrypt or validate the Keyloom secrets file." in captured.err
    for sensitive_value in (DUMMY_TOKEN, DUMMY_TRIGGER_TOKEN, DUMMY_VAULT_PASSWORD):
        assert sensitive_value not in captured.err
        assert sensitive_value not in captured.out


def test_modified_keyloom_ciphertext_fails_safely(
    tmp_path: Path,
    keyloom_envelopes: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    tampered_envelopes = dict(keyloom_envelopes)
    encrypted_value = tampered_envelopes["gitlab_trigger_token"]
    final_character = encrypted_value[-1]
    replacement = "A" if final_character != "A" else "B"
    tampered_envelopes["gitlab_trigger_token"] = f"{encrypted_value[:-1]}{replacement}"
    _write_keyloom_secrets(secrets_path, tampered_envelopes)
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with (
        patch.dict("os.environ", {"KEYLOOM_PASSWORD": DUMMY_VAULT_PASSWORD}, clear=True),
        patch.object(sys, "argv", arguments),
    ):
        with pytest.raises(SystemExit, match="2"):
            main()

    captured = capsys.readouterr()
    assert "Unable to decrypt or validate the Keyloom secrets file." in captured.err
    for sensitive_value in (DUMMY_TOKEN, DUMMY_TRIGGER_TOKEN, DUMMY_VAULT_PASSWORD):
        assert sensitive_value not in captured.out
        assert sensitive_value not in captured.err


@pytest.mark.parametrize(
    "secret_content",
    [
        "gitlab_token:\n  __keyloom__: secret\n  value: plaintext-token\n",
        "gitlab_token:\n  __keyloom__: secret\n  value: __keyloom__secret__\n",
        "gitlab_token:\n  __keyloom__: unsupported\n  value: plaintext-token\n",
        "gitlab_token:\n  __keyloom__: secret\n",
        "gitlab_token:\n  __keyloom__: secret\n  value: plaintext-token\n  extra: invalid\n",
        'gitlab_token:\n  __keyloom__: secret\n  value: "$KEYLOOM;broken"\n',
        'gitlab_token:\n  __keyloom__: secret\n  value: "$KEYLOOM;1;unknown;s=AA;n=AA;c=AA"\n',
        'gitlab_token:\n  __keyloom__: secret\n  value: "$SEAN_VAULT;1;ARGON2ID;AES256_GCM;old"\n',
    ],
)
def test_invalid_keyloom_secrets_fail_safely(
    tmp_path: Path,
    secret_content: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    secrets_path.write_text(secret_content, encoding="utf-8")
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with (
        patch.dict("os.environ", {"KEYLOOM_PASSWORD": DUMMY_VAULT_PASSWORD}, clear=True),
        patch.object(sys, "argv", arguments),
    ):
        with pytest.raises(SystemExit, match="2"):
            main()

    captured = capsys.readouterr()
    assert "Unable to decrypt or validate the Keyloom secrets file." in captured.err
    assert "plaintext-token" not in captured.out
    assert "plaintext-token" not in captured.err
    assert DUMMY_VAULT_PASSWORD not in captured.out
    assert DUMMY_VAULT_PASSWORD not in captured.err


def test_keyloom_error_details_are_not_forwarded(
    tmp_path: Path,
    keyloom_envelopes: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    _write_keyloom_secrets(secrets_path, keyloom_envelopes)
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with (
        patch.dict("os.environ", {"KEYLOOM_PASSWORD": DUMMY_VAULT_PASSWORD}, clear=True),
        patch.object(sys, "argv", arguments),
        patch(
            "clusterhop.cli.keyloom_load",
            side_effect=KeyloomError(f"unsafe details: {DUMMY_TOKEN}"),
        ),
    ):
        with pytest.raises(SystemExit, match="2"):
            main()

    captured = capsys.readouterr()
    assert "Unable to decrypt or validate the Keyloom secrets file." in captured.err
    assert DUMMY_TOKEN not in captured.out
    assert DUMMY_TOKEN not in captured.err


def test_legacy_seanvault_wrapper_is_rejected_with_migration_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    legacy_value = "$SEAN_VAULT;1;ARGON2ID;AES256_GCM;legacy-value"
    secrets_path.write_text(
        "gitlab_token:\n" "  __seanvault__: secret\n" f'  value: "{legacy_value}"\n',
        encoding="utf-8",
    )
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with patch.object(sys, "argv", arguments):
        with pytest.raises(SystemExit, match="2"):
            main()

    captured = capsys.readouterr()
    assert "Legacy Seanvault secrets are not supported" in captured.err
    assert "re-encrypt" in captured.err
    assert legacy_value not in captured.out
    assert legacy_value not in captured.err


@pytest.mark.parametrize(
    ("content", "expected_message"),
    [
        (
            "gitlab_token: first\ngitlab_token: second\n",
            "must not contain duplicate mapping keys",
        ),
        (
            "gitlab_token: &token value\ngitlab_trigger_token: *token\n",
            "must not contain YAML anchors or aliases",
        ),
        ("gitlab_token: !secret value\n", "Unable to load the secrets file"),
        (
            "gitlab_token: first\n---\ngitlab_trigger_token: second\n",
            "Unable to load the secrets file",
        ),
    ],
)
def test_secrets_yaml_rejects_unsafe_or_ambiguous_constructs(
    tmp_path: Path,
    content: str,
    expected_message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    secrets_path.write_text(content, encoding="utf-8")
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with patch.object(sys, "argv", arguments):
        with pytest.raises(SystemExit, match="2"):
            main()

    captured = capsys.readouterr()
    assert expected_message in captured.err
    assert captured.out == ""


def test_keyloom_decrypted_root_must_be_a_mapping(
    tmp_path: Path,
    keyloom_envelopes: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    secrets_path.write_text(
        "- __keyloom__: secret\n" f'  value: "{keyloom_envelopes["gitlab_token"]}"\n',
        encoding="utf-8",
    )
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with (
        patch.dict("os.environ", {"KEYLOOM_PASSWORD": DUMMY_VAULT_PASSWORD}, clear=True),
        patch.object(sys, "argv", arguments),
    ):
        with pytest.raises(SystemExit, match="2"):
            main()

    captured = capsys.readouterr()
    assert "secrets file must contain a non-empty mapping" in captured.err
    assert DUMMY_TOKEN not in captured.out
    assert DUMMY_TOKEN not in captured.err


def test_missing_secrets_file_fails_safely(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, _, _ = _write_yaml_inputs(tmp_path)
    missing_path = tmp_path / "missing-secrets.yaml"
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(missing_path),
    ]

    with patch.object(sys, "argv", arguments):
        with pytest.raises(SystemExit, match="2"):
            main()

    captured = capsys.readouterr()
    assert "Unable to load the secrets file." in captured.err
    assert str(missing_path) not in captured.err


def test_new_run_command_uses_result_exit_code(tmp_path: Path) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    backend = Mock(
        return_value=RunResult(
            status="failed",
            exit_code=1,
            summary="summary",
        )
    )
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with patch.object(sys, "argv", arguments), patch("clusterhop.cli.clusterhop_run", backend):
        with pytest.raises(SystemExit, match="1"):
            main()


@pytest.mark.parametrize(
    "arguments",
    [
        ["run", "repository", "--secrets", "secrets.yaml"],
        ["run", "--config", "config.yaml", "repository"],
        ["run", "--config", "config.yaml", "--secrets", "secrets.yaml"],
    ],
)
def test_new_run_command_rejects_missing_required_arguments(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with patch.object(sys, "argv", ["clusterhop", *arguments]):
        with pytest.raises(SystemExit) as captured:
            main()

    assert captured.value.code == 2
    assert "required" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("content", "expected_message"),
    [
        ("destination_type: [unterminated", "Unable to load the configuration file."),
        ("- not\n- a\n- mapping\n", "must contain a non-empty mapping"),
        ("", "must contain a non-empty mapping"),
    ],
)
def test_new_run_command_rejects_invalid_configuration_yaml(
    tmp_path: Path,
    content: str,
    expected_message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    config_path.write_text(content, encoding="utf-8")
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with patch.object(sys, "argv", arguments):
        with pytest.raises(SystemExit, match="2"):
            main()

    captured = capsys.readouterr()
    assert expected_message in captured.err
    assert captured.out == ""


def test_new_run_command_does_not_expose_secret_file_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    secrets_path.write_text(f"gitlab_token: [{DUMMY_TOKEN}", encoding="utf-8")
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with patch.object(sys, "argv", arguments):
        with pytest.raises(SystemExit, match="2"):
            main()

    captured = capsys.readouterr()
    assert "Unable to load the secrets file." in captured.err
    assert DUMMY_TOKEN not in captured.out
    assert DUMMY_TOKEN not in captured.err


def test_new_run_command_reports_public_configuration_errors_safely(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, _, secrets_path = _write_yaml_inputs(tmp_path)
    backend = Mock(side_effect=RunConfigurationError("Required destination field is missing."))
    arguments = [
        "clusterhop",
        "run",
        "--config",
        str(config_path),
        str(tmp_path),
        "--secrets",
        str(secrets_path),
    ]

    with patch.object(sys, "argv", arguments), patch("clusterhop.cli.clusterhop_run", backend):
        with pytest.raises(SystemExit, match="2"):
            main()

    captured = capsys.readouterr()
    assert "Required destination field is missing." in captured.err
    assert DUMMY_TOKEN not in captured.err


def test_legacy_run_command_preserves_original_copy_behavior(
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = ["clusterhop", "run", "source.py", "destination"]

    with patch.object(sys, "argv", arguments), patch("clusterhop.cli.shutil.copy") as copy:
        main()

    assert json.loads(capsys.readouterr().out.splitlines()[0]) == {"arguments": arguments}
    copy.assert_called_once_with(
        "source.py",
        "/home/work/xlab/2601-025-runner/destination.sh",
    )
