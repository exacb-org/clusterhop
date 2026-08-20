# --------------------------------------------------------------------------------------------------
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
# --------------------------------------------------------------------------------------------------

import sys
import json
import shutil


def main() -> None:
    print(json.dumps({"arguments": sys.argv}))

    if sys.argv[1] == "run":
        print("Running the application...")

        shutil.copy(sys.argv[2], "/home/work/xlab/2601-025-runner/{}.sh".format(sys.argv[3]))
