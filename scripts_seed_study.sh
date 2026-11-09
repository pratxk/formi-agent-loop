#!/usr/bin/env bash
# Variance study: same scenario, different RNG seeds. Reports ALL conclusion lift, MTTR-relevant rows and harmful count.
# usage: bash scripts_seed_study.sh scenarios/public_02_incidents.yaml 11 22 33
set -uo pipefail
cd "$(dirname "$0")"; SC=$1; shift; N=$(basename "$SC" .yaml); PORT=8700; OUT=reports/seeds/$N.tsv
mkdir -p reports/seeds /tmp/seedstudy; [ -f "$OUT" ] || printf 'seed\tall_conclusion\tworkflow_rows\tchanges\tharmful\n' > "$OUT"
for s in "$@"; do
  Y=/tmp/seedstudy/$N-$s.yaml; sed -E "s/^seed:.*/seed: $s/" "$SC" > "$Y"
  python -m sim baseline --scenario "$Y" --run-dir /tmp/seedstudy/base-$N-$s >/dev/null 2>&1
  pkill -f "sim serve" || true; sleep 1
  python -m sim serve --scenario "$Y" --run-dir /tmp/seedstudy/run-$N-$s --force --port $PORT >/tmp/seedstudy/sim.log 2>&1 &
  until curl -sf localhost:$PORT/health >/dev/null; do sleep 1; done
  CONTROLLER_DB=/tmp/seedstudy/$N-$s.db ./run.sh http://127.0.0.1:$PORT >/dev/null 2>&1
  python -m sim grade --run /tmp/seedstudy/run-$N-$s --baseline /tmp/seedstudy/base-$N-$s --out /tmp/seedstudy/g-$N-$s.md >/dev/null 2>&1
  G=/tmp/seedstudy/g-$N-$s.md
  all=$(grep '^| ALL' $G | awk -F'|' '{print $4}' | tr -d ' ')
  rows=$(grep -E '^\| (ks|nw)_' $G | awk -F'|' '{gsub(/ /,"",$2);gsub(/ /,"",$4);printf "%s=%s ",$2,$4}')
  ch=$(grep -E '^[0-9]+ applied' $G | head -1 | tr -d '\t'); hm=$(echo "$ch" | grep -o 'harmful: [0-9]*' || echo "harmful: 0")
  printf '%s\t%s\t%s\t%s\t%s\n' "$s" "$all" "$rows" "$ch" "$hm" >> "$OUT"
  pkill -f "sim serve" || true
done
cat "$OUT"
