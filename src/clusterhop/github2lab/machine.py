# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Pure helpers for translating JSC machine selections into pipeline variables."""

from __future__ import annotations

import re

_MACHINE_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class MachineNameError(ValueError):
    """Raised when a JSC machine name does not satisfy the public contract."""


def jsc_machine_pipeline_variable(machine_name: object) -> tuple[str, str]:
    """Return the GitLab trigger variable representing *machine_name*.

    Machine names are intentionally validated without sanitizing them so that distinct user
    inputs cannot silently collapse to the same pipeline-variable name.
    """

    if not isinstance(machine_name, str) or _MACHINE_NAME_PATTERN.fullmatch(machine_name) is None:
        raise MachineNameError(
            "Run configuration field jsc_machine must be a string of 1 to 64 ASCII "
            "letters, digits, or underscores, and must start with a letter."
        )

    return f"CLUSTERHOP_{machine_name.upper()}_RUN", "true"
