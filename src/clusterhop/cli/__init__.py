# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml
from keyloom import KeyloomError, load as keyloom_load  # type: ignore[import-untyped]
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from clusterhop import RunConfigurationError, run as clusterhop_run

_NEW_RUN_OPTIONS = ("--config", "--options", "--secrets")
_KEYLOOM_PASSWORD_ENVIRONMENT_VARIABLE = "KEYLOOM_PASSWORD"
_KEYLOOM_MARKER = "__keyloom__"
_SEANVAULT_MARKER = "__seanvault__"


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
        secrets = _load_secrets_mapping(parsed.secrets)
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

    return _validate_loaded_mapping(loaded, label)


def _load_secrets_mapping(path: Path) -> Mapping[str, object]:
    loaded = _load_secrets_yaml_mapping(path)
    if _contains_marker(loaded, _SEANVAULT_MARKER):
        raise CliInputError(
            "Legacy Seanvault secrets are not supported. Decrypt and re-encrypt "
            "the secrets file with Keyloom."
        )
    if not _contains_marker(loaded, _KEYLOOM_MARKER):
        return loaded

    password = os.environ.get(_KEYLOOM_PASSWORD_ENVIRONMENT_VARIABLE)
    if not password:
        raise CliInputError(
            "The KEYLOOM_PASSWORD environment variable is required for a "
            "Keyloom-marked secrets file."
        )

    try:
        resolved: object = keyloom_load(path=path, password=password)
    except (KeyloomError, OSError, UnicodeError):
        raise CliInputError("Unable to decrypt or validate the Keyloom secrets file.") from None

    return _validate_loaded_mapping(resolved, "decrypted secrets")


def _load_secrets_yaml_mapping(path: Path) -> Mapping[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
        events = yaml.parse(text, Loader=yaml.SafeLoader)
        if any(getattr(event, "anchor", None) is not None for event in events):
            raise CliInputError("The secrets file must not contain YAML anchors or aliases.")
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        if root is not None:
            _validate_unique_yaml_keys(root)
        loaded: object = yaml.safe_load(text)
    except (OSError, UnicodeError, yaml.YAMLError):
        raise CliInputError("Unable to load the secrets file.") from None

    return _validate_loaded_mapping(loaded, "secrets")


def _validate_unique_yaml_keys(node: Node) -> None:
    if isinstance(node, MappingNode):
        seen: set[tuple[str, str]] = set()
        for key_node, value_node in node.value:
            if isinstance(key_node, ScalarNode):
                identity = (key_node.tag, key_node.value)
                if identity in seen:
                    raise CliInputError("The secrets file must not contain duplicate mapping keys.")
                seen.add(identity)
            _validate_unique_yaml_keys(key_node)
            _validate_unique_yaml_keys(value_node)
    elif isinstance(node, SequenceNode):
        for item in node.value:
            _validate_unique_yaml_keys(item)


def _contains_marker(value: object, marker: str) -> bool:
    if isinstance(value, Mapping):
        if marker in value:
            return True
        return any(_contains_marker(item, marker) for item in value.values())
    if isinstance(value, list):
        return any(_contains_marker(item, marker) for item in value)
    return False


def _validate_loaded_mapping(loaded: object, label: str) -> Mapping[str, object]:
    if not isinstance(loaded, Mapping) or not loaded:
        raise CliInputError(f"The {label} file must contain a non-empty mapping.")

    result: dict[str, object] = {}
    for key, value in loaded.items():
        if not isinstance(key, str):
            raise CliInputError(f"The {label} file must use string field names.")
        result[key] = value
    return result
