"""Schedule-change check: did the hour pattern we acted on keep holding?

A schedule change moves dials toward hours that history says convert best. We cannot hold a slice of traffic
out of a schedule, and the world drifts, so a level comparison against history would blame the schedule for drift.
Instead we test the *premise*: in calls made since it went live, hours we up-weighted must not convert worse than
hours we down-weighted (within each cohort, so age mix cannot confound; level drift and caller-ID problems hit both
sides alike). If the order is clearly reversed we propose going back to uniform weights. Never when data is thin."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .. import stats
from ..governance import Governance, Policy

KIND = "schedule_rollback"
MIN_DIALS_SIDE, Z_BAD, UP, DOWN = 400, -3.0, 1.1, 0.9


def reversed_premise(db: sqlite3.Connection, wid: str, since_version: int, weights: dict, col: str) -> dict | None:
    """Stratified (per cohort) up-minus-down hour yield difference; None unless enough data on both sides."""
    cells: dict[str, dict[str, list[int]]] = {}
    for coh, hour, dials, succ in db.execute(
            f"SELECT cohort, hour, SUM(dials), SUM({col}) FROM call_agg WHERE workflow=? AND version>=? GROUP BY 1,2",
            (wid, since_version)):
        w = weights.get(coh) or weights.get("default") or {}
        side = "up" if w.get(str(hour), 1.0) >= UP else "down" if w.get(str(hour), 1.0) <= DOWN else None
        if side:
            a = cells.setdefault(coh, {"up": [0, 0], "down": [0, 0]})[side]
            a[0] += dials or 0
            a[1] += succ or 0
    num = den = 0.0
    n_up = n_dn = 0
    for c in cells.values():
        (nu, su), (nd, sd) = c["up"], c["down"]
        if nu < 50 or nd < 50:
            continue
        p = (su + sd) / (nu + nd)
        var = p * (1 - p) * (1 / nu + 1 / nd)
        if var > 0:
            diff = su / nu - sd / nd
            num, den, n_up, n_dn = num + diff / var, den + 1 / var, n_up + nu, n_dn + nd
    if min(n_up, n_dn) < MIN_DIALS_SIDE or den == 0:
        return None
    return {"diff": round(num / den, 4), "z": round(num / den * den ** 0.5, 2), "dials_up": n_up, "dials_down": n_dn}


def _same(a: dict, b: dict) -> bool:
    return a.keys() == b.keys() and all(
        a[k].keys() == b[k].keys() and all(abs(a[k][h] - b[k][h]) < 1e-3 for h in a[k]) for k in a)


def plan(db: sqlite3.Connection, gov: Governance, wf: dict, pol: Policy, cur: dict, now: datetime) -> None:
    wid = wf["workflow_id"]
    if now.hour or now.minute or gov.active(wid) or db.execute(
            "SELECT 1 FROM meta WHERE key=?", (f"sched_verdict:{wid}",)).fetchone():
        return  # one look a day; one rollback per workflow
    ap = db.execute("SELECT applied_version, config FROM proposals WHERE workflow=? AND kind='schedule_cohorts' "
                    "AND state='APPLIED' ORDER BY applied_ts DESC LIMIT 1", (wid,)).fetchone()
    if not ap or ap[0] is None:
        return
    weights = json.loads(ap[1])["schedule"]["hour_weights"]
    if not _same(cur["config"].get("schedule", {}).get("hour_weights") or {}, weights):
        return  # someone else changed the schedule since: not ours to judge or revert
    ev = reversed_premise(db, wid, ap[0], weights, "connected" if pol.primary == "pickup_rate" else "engaged")
    if not ev or ev["z"] > Z_BAD:
        return
    uniform = {k: {h: 1.0 for h in w} for k, w in weights.items()}
    cfg = json.loads(json.dumps(cur["config"]))
    cfg["schedule"]["hour_weights"] = uniform
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"sched_verdict:{wid}", json.dumps({"ts": now.isoformat(), **ev})))
    gov.propose(
        workflow=wid, kind=KIND, base_version=cur["version"], base_config=cur["config"], config=cfg,
        summary=f"Undo hour weights: up-weighted hours now convert worse than down-weighted ones (z={ev['z']})",
        diff={"hour_weights": "back to uniform"}, affected_leads=wf["daily_batch"]["size"],
        dry_run={"method": "observed: calls since the schedule change, per cohort, up-weighted vs down-weighted hours", **ev},
        impact=1.0, priority=1, now=now, evidence=ev,
        rationale=(f"The schedule assumed hours with a better history convert better. Since it went live, those hours convert "
                   f"{abs(ev['diff']):.1%} worse per dial than the hours we reduced (z={ev['z']}, {ev['dials_up']} vs "
                   f"{ev['dials_down']} dials). The premise no longer holds, so we return to equal weights. Cadence, window "
                   f"and capacity are untouched."),
        inputs={"evidence": ev, "threshold_z": Z_BAD})
