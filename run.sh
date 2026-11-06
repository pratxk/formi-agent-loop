#!/usr/bin/env bash
# Contract: ./run.sh <SIM_URL> runs until the scenario finishes. Needs Python 3.10+ and requirements.txt installed
# (no Docker needed). State lives in state/controller.db (SQLite).
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
exec python -m controller --url "$SIM_URL"
