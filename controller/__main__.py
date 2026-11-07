"""Controller entrypoint: `python -m controller --url http://127.0.0.1:8700`."""
from __future__ import annotations

import argparse
import logging
import os
import time

from datetime import datetime

from .api import Sim
from . import planners
from .governance import Governance, Policy, load_prefs
from .ingest import Ingestor
from .store import connect, meta_get, meta_set

log = logging.getLogger("controller")
TICK_MIN = 15


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8700")
    ap.add_argument("--db", default=os.environ.get("CONTROLLER_DB", "state/controller.db"))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sim = Sim(a.url)
    st = sim.get("/sim/state")
    # A restarted simulator is a new world: old state would be wrong evidence. Start clean.
    if int(meta_get(connect(a.db), "sim_minute") or 0) > st["sim_minute"]:
        log.warning("simulator is behind our store (world reset) -> starting a fresh store")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(a.db + suffix):
                os.rename(a.db + suffix, a.db + suffix + f".old{int(time.time())}")
    db = connect(a.db)
    ing = Ingestor(db, sim)
    log.info("start: sim=%s cursor=%s", st["ts"], meta_get(db, "last_chunk", "-1"))
    ing.ingest_all()  # history + anything produced before a crash
    wfs = {w["workflow_id"]: w for w in sim.get("/workflows")}
    policies = {k: Policy.of(w) for k, w in wfs.items()}
    gov = Governance(db, sim, policies, load_prefs())
    now = datetime.fromisoformat(st["ts"])
    gov.tick(now)  # reconcile anything in flight before the crash
    if meta_get(db, "planned_minute") != str(st["sim_minute"]):  # planners already ran for this tick before the crash?
        planners.run(db, sim, gov, wfs, policies, now)
    while True:
        t0 = time.time()
        status, body = sim.call("POST", "/sim/advance", json={"minutes": TICK_MIN}, retry=False)
        if status == 409:
            log.info("scenario finished")
            ing.ingest_all()
            break
        if status != 200:
            log.warning("advance failed: %s %s", status, body)
            time.sleep(1)
            continue
        t1 = time.time()
        n = ing.ingest_all()
        now = datetime.fromisoformat(body["ts"])
        planners.run(db, sim, gov, wfs, policies, now)
        meta_set(db, "planned_minute", str(body["sim_minute"]))
        gov.tick(now)
        meta_set(db, "sim_minute", str(body["sim_minute"]))
        meta_set(db, "credits", str(sim.get("/sim/state")["credits_remaining"]))
        db.execute("INSERT INTO loop_metrics VALUES(?,?,?,?,?,?)",
                   (time.strftime("%H:%M:%S"), body["ts"], n, time.time() - t1, time.time() - t0, 0))


if __name__ == "__main__":
    main()
