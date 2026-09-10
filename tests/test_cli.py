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

from clusterhop import RunConfigurationError, RunResult
from clusterhop.cli import main

DUMMY_TOKEN = "dummy-private-token-that-must-not-appear"


def _write_yaml_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    config_path = tmp_path / "config.yaml"
    options_path = tmp_path / "options.yaml"
    secrets_path = tmp_path / "secrets.yaml"
    config_path.write_text(
        "destination_type: gitlab\ngitlab_url: https://gitlab.example.test\n",
        encoding="utf-8",
    )
    options_path.write_text("poll_interval_seconds: 3\n", encoding="utf-8")
    secrets_path.write_text(
        f"gitlab_token: {DUMMY_TOKEN}\ngitlab_trigger_token: dummy-trigger-token\n",
        encoding="utf-8",
    )
    return config_path, options_path, secrets_path


def _successful_result() -> RunResult:
    return RunResult(status="success", exit_code=0, summary="summary")


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
        },
        repo_path=tmp_path,
        options={"poll_interval_seconds": 3},
        secrets={
            "gitlab_token": DUMMY_TOKEN,
            "gitlab_trigger_token": "dummy-trigger-token",
        },
    )


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
