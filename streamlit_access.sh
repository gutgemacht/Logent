#!/usr/bin/env bash
set -euo pipefail

logent_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$logent_dir"
if [[ ! -x .venv/bin/python ]]; then
    echo 'Missing Logent .venv. Create it and install requirements-dashboard.txt first.' >&2
    exit 1
fi
exec .venv/bin/python -m streamlit run streamlit_access.py --server.address 127.0.0.1 "$@"
