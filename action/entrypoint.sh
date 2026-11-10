#!/bin/sh
set -eu

git config --global --add safe.directory "${GITHUB_WORKSPACE:?}"
exec python -m clusterhop.github_action main "$@"
