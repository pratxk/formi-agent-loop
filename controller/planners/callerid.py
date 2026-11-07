"""Caller-ID health: a degrading ID is spotted by comparing it with its siblings in the same window.

Allocation of IDs is independent of lead and hour, so a sibling comparison is unconfounded and needs no
baseline that could drift. Demoted IDs keep a small probe share so recovery is observable."""
from __future__ import annotations

import copy
import sqlite3
from datetime import datetime

from .. import configdiff as cd
from .. import stats
from ..governance import Governance, Policy
from ..ingest import D0

KIND = "callerid_health"
WINDOW_H, MIN_DIALS = 24, 200
RECENT_H = 6
Z_BAD, REL_BAD, Z_OK = -3.5, 0.80, -1.5   # flag if z<-3.5 (24h) AND pickup<80% of expected over the last 6h; clear when z>-1.5
FLOOR, SPARE_PROBE = 0.05, 0.10


def _cells(db: sqlite3.Connection, wid: str, now: datetime) -> dict[tuple[int, int], dict[str, tuple[int, int]]]:
    """(day, hour) -> caller_id -> (dials, connected) over the window. Cells keep pickup comparisons honest: allocation
    changes during the window, and pickup also depends on hour and day, so comparing raw window totals would blame
    an ID for simply having been used at bad hours."""
    idx = (now.date() - D0).days * 24 + now.hour
    out: dict[tuple[int, int], dict[str, tuple[int, int]]] = {}
    for d, h, cid, n, c in db.execute(
            "SELECT day, hour, caller_id, SUM(dials), SUM(connected) FROM call_agg WHERE workflow=? AND day*24+hour>? "
            "GROUP BY 1,2,3", (wid, idx - WINDOW_H)):
        out.setdefault((d, h), {})[cid] = (n, c)
    return out


def stratified(cells: dict, i: str, rest: set[str]) -> tuple[float, float, int, int]:
    """Cochran-Mantel-Haenszel style: z and observed/expected pickup of ID i vs the other active IDs, within cells."""
    o = e = v = 0.0
    n_i = n_r = 0
    for per in cells.values():
        ni, ci = per.get(i, (0, 0))
        nr = sum(per.get(j, (0, 0))[0] for j in rest)
        cr = sum(per.get(j, (0, 0))[1] for j in rest)
        n_i, n_r = n_i + ni, n_r + nr
        big_n, big_c = ni + nr, ci + cr
        if ni == 0 or nr == 0 or big_n < 2:
            continue
        o += ci
        e += ni * big_c / big_n
        v += ni * nr * big_c * (big_n - big_c) / (big_n * big_n * (big_n - 1))
    z = (o - e) / v ** 0.5 if v > 0 else 0.0
    return z, (o / e if e > 0 else 1.0), n_i, n_r


def plan(db: sqlite3.Connection, gov: Governance, wf: dict, pol: Policy, cur: dict, now: datetime) -> None:
    if now.minute != 0:  # hourly evaluation keeps the false-alarm budget small (tests are correlated across ticks)
        return
    wid = wf["workflow_id"]
    cfg = copy.deepcopy(cur["config"])
    dist = cd.effective_dist(cfg, "default", "caller_id")
    active = [i for i, w in dist.items() if w > 0]
    if len(active) < 2:
        return
    cells = _cells(db, wid, now)
    now_idx = (now.date() - D0).days * 24 + now.hour
    verdict: dict[str, str] = {}
    for i in active:
        rest = set(active) - {i}
        z, _, n, rn = stratified(cells, i, rest)
        recent = {k: v for k, v in cells.items() if k[0] * 24 + k[1] > now_idx - RECENT_H}
        ratio = stratified(recent, i, rest)[1]  # practical floor on the recent window: a 24h average dilutes a fresh collapse
        if n < MIN_DIALS or rn < MIN_DIALS:
            continue
        fair = 1 / len(active)
        demoted = dist[i] < 0.5 * fair
        if z < Z_BAD and ratio < REL_BAD:
            verdict[i] = "bad"
        elif demoted and z > Z_OK:
            verdict[i] = "ok"
        elif not demoted:
            verdict[i] = "ok"
    bad = {i for i, v in verdict.items() if v == "bad"}
    recovered = {i for i in active if verdict.get(i) == "ok" and dist[i] < 0.5 / len(active)}
    if not bad and not recovered:
        return
    if len(bad) == len(active):
        return  # everything looks bad together: that is not a caller-ID problem
    demoted_now = {i for i in active if dist[i] < 0.5 / len(active)} - recovered
    flagged = (demoted_now | bad)
    spares = [o for o in wf["options"]["caller_id"] if dist.get(o, 0) == 0]
    target = {i: FLOOR for i in flagged}
    probe = spares[0] if flagged and spares and not any(dist.get(s, 0) for s in spares) else None
    if probe:
        target[probe] = SPARE_PROBE
    good = [i for i in active if i not in flagged]
    rest = 1 - sum(target.values())
    for i in good:
        target[i] = rest / len(good)
    if probe is None:  # keep an already-active probe where it is
        for s in spares:
            if dist.get(s, 0) > 0:
                target[s] = dist[s]
    new = _step(dist, target, pol if pol.auto_enabled and "caller_id" in pol.auto_dims else None)
    cfg.setdefault("allocations", {}).setdefault("default", {})["caller_id"] = {k: round(v, 4) for k, v in new.items() if v > 0}
    tot = {i: [sum(c.get(i, (0, 0))[k] for c in cells.values()) for k in (0, 1)] for i in sorted(set(active) | set(spares))}
    verdict_txt = tot
    gov.propose(
        workflow=wid, kind=KIND, base_version=cur["version"], base_config=cur["config"], config=cfg,
        summary=f"Rotate calls away from caller ID {', '.join(sorted(bad or flagged))} (pickup collapsed vs siblings)",
        diff={"caller_id": cfg["allocations"]["default"]["caller_id"]},
        affected_leads=wf["daily_batch"]["size"],
        dry_run={"window_hours": WINDOW_H, "window_dials_connected": verdict_txt,
                 "note": "kept a probe share on the demoted ID so recovery is measurable"},
        impact=min(1.0, 0.5 + 0.1 * len(flagged)),
        rationale=(f"Over the last {WINDOW_H}h {sorted(bad)} picked up far below siblings (z<{Z_BAD}, <{REL_BAD:.0%} of siblings' rate)."
                   if bad else f"Continuing staged rotation / recovery for {sorted(flagged)}."),
        inputs={"window": verdict_txt, "bad": sorted(bad), "recovered": sorted(recovered), "z_bad": Z_BAD},
        now=now, priority=1)


def _step(cur: dict[str, float], target: dict[str, float], pol: Policy | None) -> dict[str, float]:
    """Move toward the target, in steps that respect the policy's per-change total-variation cap if auto-applying."""
    keys = set(cur) | set(target)
    tv = 0.5 * sum(abs(cur.get(k, 0) - target.get(k, 0)) for k in keys)
    cap = pol.auto_shift * 0.9 if pol else 1.0
    a = 1.0 if tv <= cap or tv == 0 else cap / tv
    return {k: cur.get(k, 0) + a * (target.get(k, 0) - cur.get(k, 0)) for k in keys}
