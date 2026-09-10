# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from clusterhop import RunConfigurationError, run as clusterhop_run

_NEW_RUN_OPTIONS = ("--config", "--options", "--secrets")


class CliInputError(ValueError):
    """A user-facing, secret-safe CLI input error."""


def main() -> None:
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "run" and not _is_legacy_run(arguments):
        raise SystemExit(_run_command(arguments[1:]))

    _run_legacy_command()


def _is_legacy_run(arguments: Sequence[str]) -> bool:
    return (
        len(arguments) == 3
        and arguments[0] == "run"
        and not any(
            argument == option or argument.startswith(f"{option}=")
            for argument in arguments[1:]
            for option in _NEW_RUN_OPTIONS
        )
    )


def _run_legacy_command() -> None:
    """Preserve the original positional command behavior exactly."""

    print(json.dumps({"arguments": sys.argv}))

    if sys.argv[1] == "run":
        print("Running the application...")

        shutil.copy(sys.argv[2], "/home/work/xlab/2601-025-runner/{}.sh".format(sys.argv[3]))


def _run_command(arguments: Sequence[str]) -> int:
    parser = _run_argument_parser()
    parsed = parser.parse_args(arguments)

    try:
        config = _load_yaml_mapping(parsed.config, "configuration")
        options = (
            _load_yaml_mapping(parsed.options, "options") if parsed.options is not None else None
        )
        secrets = _load_yaml_mapping(parsed.secrets, "secrets")
        result = clusterhop_run(
            config=config,
            repo_path=parsed.repo_path,
            options=options,
            secrets=secrets,
        )
    except (CliInputError, RunConfigurationError) as error:
        print(f"ClusterHop error: {error}", file=sys.stderr)
        return 2

    print(f"ClusterHop run finished with status: {result.status}")
    return result.exit_code


def _run_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clusterhop run",
        description="Run ClusterHop using already-defined YAML inputs.",
    )
    parser.add_argument("--config", type=Path, required=True, help="configuration YAML file")
    parser.add_argument("--options", type=Path, help="optional runtime options YAML file")
    parser.add_argument("repo_path", type=Path, metavar="REPO_PATH")
    parser.add_argument("--secrets", type=Path, required=True, help="secrets YAML file")
    return parser


def _load_yaml_mapping(path: Path, label: str) -> Mapping[str, object]:
    try:
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise CliInputError(f"Unable to load the {label} file.") from None

    if not isinstance(loaded, Mapping) or not loaded:
        raise CliInputError(f"The {label} file must contain a non-empty mapping.")

    result: dict[str, object] = {}
    for key, value in loaded.items():
        if not isinstance(key, str):
            raise CliInputError(f"The {label} file must use string field names.")
        result[key] = value
    return result
