"""Call-time planner: who gets dialled in which hour. Learns hour x segment yield from pre-D0 history.

Hour weights only re-order *who* the dialler picks (the business owns how many calls and when the window is),
so this stays inside the constraints. Segments become cohorts only when the hour profile truly differs by
segment (chi-square gate); that keeps traffic from fragmenting for no reason."""
from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime

from .. import stats
from ..governance import Governance, Policy

FEATURES = ("age_band", "lang")
MIN_SEG_DIALS = 2000     # below this a segment is too thin to get its own hour table
SHRINK = 100             # pseudo-dials pulling a thin hour toward the segment mean
POWER, W_MIN, W_MAX = 2.0, 0.25, 3.0
KIND = "schedule_cohorts"


def _profile(db: sqlite3.Connection, wf: str, feat: str) -> dict[str, dict[int, tuple[int, int, int]]]:
    out: dict[str, dict[int, tuple[int, int, int]]] = {}
    for seg, h, d, c, e in db.execute(
            f"SELECT {feat}, hour, SUM(dials), SUM(connected), SUM(engaged) FROM hist_agg WHERE workflow=? "
            f"GROUP BY 1,2", (wf,)):
        out.setdefault(seg, {})[h] = (d, c, e)
    return out


def _success_idx(primary: str) -> int:
    return 1 if primary == "pickup_rate" else 2  # connected for pickup-primary, engaged-per-dial otherwise


def _weights(prof: dict[int, tuple[int, int, int]], idx: int) -> dict[int, float]:
    d = sum(v[0] for v in prof.values())
    pbar = sum(v[idx] for v in prof.values()) / d
    w = {}
    for h, v in prof.items():
        p = (v[idx] + SHRINK * pbar) / (v[0] + SHRINK)
        w[h] = min(W_MAX, max(W_MIN, (p / pbar) ** POWER))
    m = sum(w.values()) / len(w)
    return {h: round(x / m, 3) for h, x in w.items()}  # mean 1 so cohorts compete on equal footing


def _expect(prof: dict[int, tuple[int, int, int]], w: dict[int, float] | None) -> tuple[float, float, float]:
    """Expected (pickup, engagement, yield) if dial share per hour is scaled by w (supply per hour unchanged)."""
    share = {h: v[0] * (w[h] if w else 1.0) for h, v in prof.items()}
    z = sum(share.values())
    pick = sum(share[h] * v[1] / v[0] for h, v in prof.items()) / z
    eng_num = sum(share[h] * v[2] / v[0] for h, v in prof.items()) / z
    return pick, (eng_num / pick if pick else 0.0), eng_num


def plan(db: sqlite3.Connection, gov: Governance, wf: dict, pol: Policy, cur: dict, now: datetime) -> None:
    wid = wf["workflow_id"]
    if _blocked(db, gov, wid):
        return
    idx = _success_idx(pol.primary)
    best, best_stat = None, 0.0
    for feat in FEATURES:  # which feature, if any, explains hour-profile differences?
        prof = _profile(db, wid, feat)
        prof = {s: p for s, p in prof.items() if sum(v[0] for v in p.values()) >= MIN_SEG_DIALS}
        if len(prof) < 2:
            continue
        x, df = stats.interaction_chi2({s: {h: (v[0], v[idx]) for h, v in p.items()} for s, p in prof.items()})
        if stats.chi2_sf(x, df) < 1e-6 and x / df > best_stat:
            best, best_stat = (feat, prof, x, df), x / df
    pooled = {}
    for p in _profile(db, wid, "age_band").values():
        for h, v in p.items():
            a = pooled.get(h, (0, 0, 0))
            pooled[h] = (a[0] + v[0], a[1] + v[1], a[2] + v[2])
    if not pooled:
        return
    cfg = copy.deepcopy(cur["config"])
    sched = {"default": _weights(pooled, idx)}
    cohorts, before, after, evidence = [], _expect(pooled, None), None, {}
    if best:
        feat, prof, x, df = best
        cohorts = [{"id": f"{feat}_{s}".replace("+", "plus").replace("-", "_"), "where": {feat: [s]}} for s in sorted(prof)]
        for c, s in zip(cohorts, sorted(prof)):
            sched[c["id"]] = _weights(prof[s], idx)
        tot = sum(sum(v[0] for v in p.values()) for p in prof.values())
        exp = [(sum(v[0] for v in p.values()), _expect(p, sched[c["id"]]), _expect(p, None))
               for (s, p), c in zip(sorted(prof.items()), cohorts)]
        after = tuple(sum(n * aft[i] for n, aft, _ in exp) / tot for i in range(3))
        before = tuple(sum(n * bef[i] for n, _, bef in exp) / tot for i in range(3))
        evidence = {"gate_feature": feat, "chi2": round(x, 1), "df": df}
        cfg["cohorts"] = cohorts
    else:
        after = _expect(pooled, sched["default"])
    cfg["schedule"] = {**cfg.get("schedule", {}), "hour_weights": sched}
    lift = (after[2] - before[2]) / before[2] if before[2] else 0.0
    guard_ok = _guardrail_ok(pol, before, after)
    if lift < 0.02 or not guard_ok:
        gov.db.execute("INSERT INTO decisions(ts,workflow,kind,inputs,outcome) VALUES(?,?,?,?,?)",
                       (now.isoformat(), wid, KIND, json.dumps({"lift": lift, "guardrail_ok": guard_ok}),
                        "no schedule change: predicted lift too small or guardrail at risk"))
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"calltime_checked:{wid}", now.isoformat()))
        return
    names = ", ".join(c["id"] for c in cohorts) or "all leads"
    gov.propose(
        workflow=wid, kind=KIND, base_version=cur["version"], base_config=cur["config"], config=cfg,
        summary=f"Call people in the hours they pick up: hour weights for {names}",
        diff={"cohorts": [c["id"] for c in cohorts], "hour_weights": sched},
        affected_leads=wf["daily_batch"]["size"],
        dry_run={"method": "replay of pre-D0 history with dial share per hour scaled by the weights (estimate; assumes hourly "
                           "slot supply unchanged)",
                 "before": {"pickup": round(before[0], 4), "engagement": round(before[1], 4), "yield": round(before[2], 4)},
                 "after": {"pickup": round(after[0], 4), "engagement": round(after[1], 4), "yield": round(after[2], 4)}},
        impact=min(1.0, lift / 0.15),
        rationale=(f"History shows pickup depends on hour"
                   + (f" differently by {best[0]} (chi2={best[2]:.0f}, df={best[3]})" if best else "")
                   + f". Expected yield per dial +{lift:.1%}. No change to cadence, window or capacity."),
        inputs={"success_kpi": "connected" if idx == 1 else "engaged", "gate": evidence, "predicted_lift": round(lift, 4)},
        now=now, priority=0)


def _guardrail_ok(pol: Policy, before: tuple, after: tuple) -> bool:
    g = pol.guardrail
    for k, cap in g.items():
        if k == "engagement_rate_max_drop" and before[1] - after[1] > cap / 2:
            return False
        if k == "pickup_rate_max_drop" and before[0] - after[0] > cap / 2:
            return False
    return True


def _blocked(db: sqlite3.Connection, gov: Governance, wid: str) -> bool:
    """Plan once; re-plan only after a failed attempt, and never over a supervisor's schedule edit."""
    if db.execute("SELECT 1 FROM sim_events WHERE type='config_changed' AND workflow=? AND body LIKE '%\"supervisor\"%' "
                  "AND body LIKE '%schedule%'", (wid,)).fetchone():
        return True
    if db.execute("SELECT 1 FROM meta WHERE key=?", (f"calltime_checked:{wid}",)).fetchone():
        return True
    return bool(db.execute("SELECT 1 FROM proposals WHERE workflow=? AND kind=? AND state IN "
                           "('QUEUED','BATCHED','SUBMITTING','SUBMITTED','APPROVED','APPLYING','APPLIED')",
                           (wid, KIND)).fetchone())
