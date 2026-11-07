#!/usr/bin/env bash
# Kill the controller (once, hard) at the three riskiest instants: right after a side effect reached the simulator
# and before we recorded it. Restart and compare with an uninterrupted run: same proposals, same final configs.
set -euo pipefail
cd "$(dirname "$0")"
PORT=${PORT:-8700}; SC=${SCENARIO:-scenarios/public_02_incidents.yaml}
fresh_sim() { pkill -f "sim serve" || true; sleep 1; python -m sim serve --scenario "$SC" --run-dir runs/crashpt --force --port $PORT >/tmp/sim_cp.log 2>&1 & until curl -sf localhost:$PORT/health >/dev/null; do sleep 1; done; }
summary() { sqlite3 "$1" "select group_concat(kind||':'||state||':'||n) from (select kind,state,count(*) n from proposals group by 1,2 order by 1,2); select sum(connected) from call_agg;" | tr '\n' ' '; for w in nw_renewal nw_kyc ks_cancel; do curl -s localhost:$PORT/workflows/$w/config | python3 -c "import json,sys;print('v'+str(json.load(sys.stdin)['version']),end=' ')"; done; }
rm -rf state_cp; fresh_sim; CONTROLLER_DB=state_cp/ref.db ./run.sh http://127.0.0.1:$PORT >/dev/null 2>&1; REF=$(summary state_cp/ref.db)
echo "reference: $REF"; rc=0
for label in after_submit after_apply after_auto_put; do
  fresh_sim; rm -f state_cp/mark; export CONTROLLER_CRASH=$label CONTROLLER_CRASH_MARK=state_cp/mark
  set +e; CONTROLLER_DB=state_cp/$label.db ./run.sh http://127.0.0.1:$PORT >/dev/null 2>&1; code=$?; set -e
  [ -f state_cp/mark ] || { echo "$label: crash point never reached"; rc=1; continue; }
  unset CONTROLLER_CRASH CONTROLLER_CRASH_MARK
  CONTROLLER_DB=state_cp/$label.db ./run.sh http://127.0.0.1:$PORT >/dev/null 2>&1
  GOT=$(summary state_cp/$label.db)
  [ "$GOT" = "$REF" ] && echo "PASS $label (died with exit $code, recovered): identical" || { echo "FAIL $label: $GOT"; rc=1; }
done
exit $rc
