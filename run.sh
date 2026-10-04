#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ -x .venv/bin/python ]]; then
    python_bin=.venv/bin/python
elif [[ -x venv/bin/python ]]; then
    python_bin=venv/bin/python
else
    python_bin=python3
fi
export PYTHONDONTWRITEBYTECODE=1
exec "$python_bin" main.py "$@"
