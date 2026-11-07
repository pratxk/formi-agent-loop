#!/usr/bin/env bash
# Kill the controller mid-run (3x), restart each time, and compare the final store to an uninterrupted run.
set -euo pipefail
cd "$(dirname "$0")"
PORT=${PORT:-8700}; SC=${SCENARIO:-scenarios/public_01_dev.yaml}
fresh_sim() { pkill -f "sim serve" || true; sleep 1; python -m sim serve --scenario "$SC" --run-dir runs/crash --force --port $PORT >/tmp/sim_crash.log 2>&1 & sleep 7; }
summary() { sqlite3 "$1" "select count(*), sum(connected), sum(engaged) from call_audit; select sum(dials),sum(connected),sum(engaged) from call_agg; select value from meta where key='last_chunk'; select group_concat(kind||':'||state||':'||n) from (select kind,state,count(*) n from proposals group by 1,2 order by 1,2);" | tr '\n' ' '; for w in nw_renewal nw_kyc ks_cancel; do curl -s localhost:$PORT/workflows/$w/config | python3 -c "import json,sys;print('v'+str(json.load(sys.stdin)['version']),end=' ')"; done; }
rm -rf state_crash state_ref; mkdir -p reports
fresh_sim; CONTROLLER_DB=state_ref/c.db ./run.sh http://127.0.0.1:$PORT >/dev/null 2>&1; REF=$(summary state_ref/c.db)
fresh_sim
for i in 1 2 3; do
  CONTROLLER_DB=state_crash/c.db ./run.sh http://127.0.0.1:$PORT >/dev/null 2>&1 & PID=$!
  sleep 1.6; kill -9 $PID 2>/dev/null || true; wait $PID 2>/dev/null || true
  echo "kill #$i at sim_minute=$(curl -s localhost:$PORT/sim/state | python3 -c 'import json,sys;print(json.load(sys.stdin)["sim_minute"])')"
done
CONTROLLER_DB=state_crash/c.db ./run.sh http://127.0.0.1:$PORT >/dev/null 2>&1
GOT=$(summary state_crash/c.db)
echo "reference : $REF"; echo "after kills: $GOT"
[ "$REF" = "$GOT" ] && echo "PASS: identical" || { echo "FAIL"; exit 1; }
