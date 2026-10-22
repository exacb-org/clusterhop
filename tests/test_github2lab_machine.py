# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

"""Tests for dynamic JSC machine trigger-variable generation."""

from __future__ import annotations

import pytest

from clusterhop.github2lab.machine import MachineNameError, jsc_machine_pipeline_variable


@pytest.mark.parametrize(
    ("machine_name", "expected_name"),
    [
        ("jupiter", "CLUSTERHOP_JUPITER_RUN"),
        ("jureca", "CLUSTERHOP_JURECA_RUN"),
        ("juwels", "CLUSTERHOP_JUWELS_RUN"),
        ("new_machine", "CLUSTERHOP_NEW_MACHINE_RUN"),
        ("JUPITER", "CLUSTERHOP_JUPITER_RUN"),
        ("JuPiTeR", "CLUSTERHOP_JUPITER_RUN"),
        ("machine_2", "CLUSTERHOP_MACHINE_2_RUN"),
        ("a" * 64, f"CLUSTERHOP_{'A' * 64}_RUN"),
    ],
)
def test_jsc_machine_pipeline_variable_is_dynamic_and_deterministic(
    machine_name: str,
    expected_name: str,
) -> None:
    assert jsc_machine_pipeline_variable(machine_name) == (expected_name, "true")
    assert jsc_machine_pipeline_variable(machine_name) == (expected_name, "true")


@pytest.mark.parametrize(
    "machine_name",
    [
        "",
        " jupiter",
        "jupiter ",
        "jupiter west",
        "new-machine",
        "2jupiter",
        "jupiter!",
        "jüpiter",
        "a" * 65,
        1,
        True,
        ["jupiter"],
        {"name": "jupiter"},
        None,
    ],
)
def test_jsc_machine_pipeline_variable_rejects_invalid_values(machine_name: object) -> None:
    with pytest.raises(MachineNameError, match="jsc_machine"):
        jsc_machine_pipeline_variable(machine_name)
