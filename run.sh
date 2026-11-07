#!/usr/bin/env bash
# Contract: ./run.sh <SIM_URL> runs until the scenario finishes. Python 3.10+, no Docker. If the dependencies are
# missing it installs requirements.txt into .venv first (the only "setup", and it is automatic).
# State lives in state/controller.db (SQLite); override with CONTROLLER_DB.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
PY=python3; [ -x .venv/bin/python ] && PY=.venv/bin/python
if ! "$PY" -c "import httpx, yaml" 2>/dev/null; then
  python3 -m venv .venv && PY=.venv/bin/python
  "$PY" -m pip install -q -r requirements.txt
fi
exec "$PY" -m controller --url "$SIM_URL"
