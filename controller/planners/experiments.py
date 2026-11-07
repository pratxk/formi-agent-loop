"""Welcome-message / voice experiments, per cohort, with deterministic winner selection and a decay holdout.

Welcome and voice act after the call connects, so they can only move engagement. Workflows whose primary KPI is
pickup are therefore left alone (no traffic spent on a lever that cannot move the primary).

Life of one (cohort, dim):  start -> explore (champion keeps 40%) -> promote winner (old champion keeps a 15%
holdout) -> rollback if the holdout shows the lift is gone. No LLM is involved; every threshold is a constant.
State is a small JSON in `meta`, written before the proposal that depends on it. `pending` becomes `cur` only when
the live config matches it, so a rejected / expired proposal can never be mistaken for a running experiment."""
from __future__ import annotations

import copy
import json
import math
import sqlite3
from datetime import datetime, timedelta

from .. import configdiff as cd
from .. import stats
from ..governance import Governance, Policy
from ..ingest import D0

DIMS = (("welcome_message", "welcome"), ("voice", "voice"))
KIND, ROLLBACK = "experiment", "rollback"
CHAMPION_SHARE, HOLDOUT_SHARE = 0.40, 0.15
ALPHA, LOOKS = 0.10, 6          # family alpha per WORKFLOW, split over its (cohort, dim) pairs and 6 daily looks
MIN_N, MIN_LIFT = 300, 0.02     # connected calls per arm; practical lift floor (absolute engagement)
FUTILE_Z, LATE_FRAC, ROLLBACK_Z, MIN_HOLD_N, RECENT_DAYS = -2.0, 0.4, -1.0, 100, 2
SUPERVISOR_HOLD_H = 48


def _key(wid: str, cohort: str, dim: str) -> str:
    return f"exp:{wid}:{cohort}:{dim}"


def _state(db: sqlite3.Connection, k: str) -> dict:
    r = db.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    return json.loads(r[0]) if r else {}


def _save(db: sqlite3.Connection, k: str, v: dict) -> None:
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, json.dumps(v, sort_keys=True)))


def tv(p: dict[str, float], q: dict[str, float]) -> float:
    return 0.5 * sum(abs(p.get(k, 0) - q.get(k, 0)) for k in set(p) | set(q))


def _rows(db: sqlite3.Connection, wid: str, cohort: str, col: str) -> list[tuple]:
    return db.execute(
        f"SELECT version, day, {col}, SUM(dials), SUM(connected), SUM(engaged) FROM call_agg "
        "WHERE workflow=? AND cohort=? GROUP BY 1,2,3", (wid, cohort)).fetchall()


def compare(rows: list[tuple], a: str, b: str, since_version: int = 0, since_day: int = 0) -> dict | None:
    """Arm a vs arm b using only config versions in which BOTH were live (contemporaneous, so time trends and
    novelty effects cannot masquerade as an arm effect). Returns counts, lift, z and an early/late split."""
    by: dict[int, dict[str, dict[int, tuple[int, int, int]]]] = {}
    for v, d, o, dials, con, eng in rows:
        if v >= since_version and d >= since_day and o in (a, b):
            by.setdefault(v, {}).setdefault(o, {})[d] = (dials, con, eng)
    days_a: dict[int, list[int]] = {}
    days_b: dict[int, list[int]] = {}
    for per in by.values():
        if a in per and b in per:
            for days, arm in ((days_a, a), (days_b, b)):
                for d, t in per[arm].items():
                    acc = days.setdefault(d, [0, 0, 0])
                    for i in range(3):
                        acc[i] += t[i]
    if not days_a or not days_b:
        return None
    def tot(days: dict[int, list[int]], sel=lambda d: True) -> list[int]:
        return [sum(t[i] for d, t in days.items() if sel(d)) for i in range(3)]
    all_days = sorted(set(days_a) | set(days_b))
    mid = (all_days[0] + all_days[-1]) / 2
    ta, tb = tot(days_a), tot(days_b)
    out = {"n_a": ta[1], "n_b": tb[1], "x_a": ta[2], "x_b": tb[2], "dials_a": ta[0], "dials_b": tb[0]}
    if ta[1] == 0 or tb[1] == 0:
        return None
    out["rate_a"], out["rate_b"] = ta[2] / ta[1], tb[2] / tb[1]
    out["lift"] = out["rate_a"] - out["rate_b"]
    out["z"] = stats.two_prop_z(ta[2], ta[1], tb[2], tb[1])
    out["pickup_diff"] = ta[1] / max(ta[0], 1) - tb[1] / max(tb[0], 1)
    out["pickup_z"] = stats.two_prop_z(ta[1], ta[0], tb[1], tb[0])
    la, lb = tot(days_a, lambda d: d > mid), tot(days_b, lambda d: d > mid)
    out["late_n"] = min(la[1], lb[1])
    out["late_lift"] = (la[2] / la[1] - lb[2] / lb[1]) if la[1] and lb[1] else None
    return out


def holm_winners(cmp: dict[str, dict], pairs: int = 1) -> list[str]:
    """Holm step-down over challengers at the per-look alpha. Returns challengers that are significantly better."""
    alpha = ALPHA / LOOKS / pairs
    ps = sorted((stats.norm_sf(c["z"]), a) for a, c in cmp.items())
    out = []
    for i, (p, a) in enumerate(ps):
        if p > alpha / (len(ps) - i):
            break
        out.append(a)
    return out


def guardrail_ok(pol: Policy, c: dict) -> bool:
    cap = pol.guardrail.get("pickup_rate_max_drop")
    if cap is None:
        return True
    se = math.sqrt(0.25 / max(c["dials_a"], 1) + 0.25 / max(c["dials_b"], 1))  # conservative (p=.5) variance
    return c["pickup_diff"] + 1.645 * se > -cap


def _explore_dist(champion: str, options: list[str], live: list[str]) -> dict[str, float]:
    others = [o for o in options if o != champion and o in live]
    if not others:
        return {champion: 1.0}
    return {champion: CHAMPION_SHARE, **{o: round((1 - CHAMPION_SHARE) / len(others), 4) for o in others}}


def decide(rows: list[tuple], st: dict, options: list[str], pol: Policy, cur_dist: dict[str, float],
           pairs: int = 1) -> dict | None:
    """Pure decision for one (cohort, dim) at a daily look. Returns {dist, phase, kind, why, ...} or None."""
    ph = st.get("phase")
    if ph == "explore":
        ch = st["champion"]
        live = [o for o in cur_dist if o != ch and cur_dist[o] > 0]
        cmp = {o: c for o in live if (c := compare(rows, o, ch, st.get("since_version", 0))) and c["n_a"] >= MIN_N
               and c["n_b"] >= MIN_N}
        if not cmp:
            return None
        win = [a for a in holm_winners(cmp, pairs) if cmp[a]["lift"] >= MIN_LIFT and guardrail_ok(pol, cmp[a])
               and (cmp[a]["late_lift"] is None or cmp[a]["late_n"] < 100 or cmp[a]["late_lift"] >= LATE_FRAC * cmp[a]["lift"])]
        if win:
            w = max(win, key=lambda a: cmp[a]["lift"])
            return {"phase": "promoted", "kind": KIND, "dist": {w: 1 - HOLDOUT_SHARE, ch: HOLDOUT_SHARE},
                    "winner": w, "baseline": ch, "evidence": cmp[w], "why": f"{w} beats {ch}", "all": cmp}
        futile = [o for o, c in cmp.items() if c["z"] < FUTILE_Z]
        if futile:
            left = [o for o in live if o not in futile]
            return {"phase": "explore" if left else "done", "kind": KIND, "dist": _explore_dist(ch, options, left),
                    "champion": ch, "why": f"dropped {futile}: clearly worse than {ch}", "all": cmp, "dropped": futile}
        return None
    return None


def pooled_decay(per_cohort: dict[str, tuple[dict, list[tuple]]], now_day: int) -> dict[str, dict]:
    """Decay check pooled over every cohort where the same variant was promoted (a variant that stops working tends to
    stop everywhere; pooling is what gives the 15% holdouts enough calls to see it). Looks at the last RECENT_DAYS only,
    so early good days cannot mask a fade. Returns {cohort: evidence} for cohorts to roll back (empty = healthy)."""
    groups: dict[tuple, list[tuple[str, dict]]] = {}
    for c, (st, rows) in per_cohort.items():
        if st.get("phase") != "promoted":
            continue
        cmp = compare(rows, st["winner"], st["baseline"], st.get("since_version", 0), max(0, now_day - RECENT_DAYS))
        if cmp:
            groups.setdefault((st["winner"], st["baseline"]), []).append((c, cmp))
    out: dict[str, dict] = {}
    for (w, base), items in groups.items():
        n_hold = sum(c["n_b"] for _, c in items)
        ws = [math.sqrt(min(c["n_a"], c["n_b"])) for _, c in items]
        z = sum(wi * c["z"] for wi, (_, c) in zip(ws, items)) / math.sqrt(sum(wi * wi for wi in ws))
        if n_hold >= MIN_HOLD_N and z < ROLLBACK_Z:
            for c, cmp in items:
                out[c] = {**cmp, "pooled_z": round(z, 2), "pooled_holdout_n": n_hold, "winner": w, "baseline": base}
    return out


def _settled(wid: str, gov: Governance) -> bool:
    """Do not start experimenting while a cohort/schedule change is still in flight: it moves the cohort ids."""
    return not gov.active(wid, "schedule_cohorts")


def _hold(db: sqlite3.Connection, wid: str, now: datetime) -> bool:
    r = db.execute("SELECT value FROM meta WHERE key=?", (f"exp_hold:{wid}",)).fetchone()
    return bool(r and now < datetime.fromisoformat(r[0]))


def plan(db: sqlite3.Connection, gov: Governance, wf: dict, pol: Policy, cur: dict, now: datetime) -> None:
    wid, cfg = wf["workflow_id"], cur["config"]
    if pol.primary != "engagement_rate":
        return
    if _supervisor_edit(db, gov, wf, cur, now):
        return
    cohorts = ["default"] + [c["id"] for c in cfg.get("cohorts", [])]
    version = cur["version"]
    # 1. reconcile our records with what is live
    for c in cohorts:
        for dim, _ in DIMS:
            k, st = _key(wid, c, dim), _state(db, _key(wid, c, dim))
            live = cd.effective_dist(cfg, c, dim)
            p = st.get("pending")
            if p and tv(live, p["dist"]) <= 0.02:
                st = {"cur": {**p, "since_version": version}}
                _save(db, k, st)
                continue
            if p and not gov.active(wid):
                st.pop("pending")
                _save(db, k, st)
            if st.get("cur") and tv(live, st["cur"]["dist"]) > 0.02:   # someone else changed it: never revert them
                db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)",
                           (f"exp_hold:{wid}", (now + timedelta(hours=SUPERVISOR_HOLD_H)).isoformat()))
                db.execute("INSERT INTO decisions(ts,workflow,kind,inputs,outcome) VALUES(?,?,?,?,?)",
                           (now.isoformat(), wid, KIND, json.dumps({"cohort": c, "dim": dim, "live": live}),
                            "allocation changed outside the loop (supervisor): experiment state reset, no revert"))
                db.execute("DELETE FROM meta WHERE key=?", (k,))
                continue
    if _hold(db, wid, now) or not _settled(wid, gov) or gov.active(wid, KIND) or gov.active(wid, ROLLBACK):
        return
    daily_look = now.hour == 0 and now.minute == 0
    new_cfg, changes = copy.deepcopy(cfg), []
    owned = set(json.loads((db.execute("SELECT value FROM meta WHERE key=?", (f"exp_owned:{wid}",)).fetchone() or ["[]"])[0]))
    for c in cohorts:
        for dim, col in DIMS:
            if dim in owned:
                continue
            opts = wf["options"].get(dim, [])
            k, st = _key(wid, c, dim), _state(db, _key(wid, c, dim))
            live = cd.effective_dist(cfg, c, dim)
            cu = st.get("cur")
            if cu is None:
                top = max(live, key=live.get) if live else None
                if len(opts) < 2 or top is None or live[top] < 0.95:
                    continue
                d = {"phase": "explore", "kind": KIND, "champion": top, "dist": _explore_dist(top, opts, opts),
                     "why": f"start exploring {dim}: {top} is champion, no history exists for the alternatives"}
            elif daily_look:
                d = decide(_rows(db, wid, c, col), {**cu, "since_version": cu.get("since_version", 0)}, opts, pol, live, len(cohorts) * len(DIMS))
            else:
                d = None
            if d:
                changes.append((c, dim, k, st, d))
                new_cfg.setdefault("allocations", {}).setdefault(c, {})[dim] = d["dist"]
    if daily_look:
        for dim, col in DIMS:
            per = {c: (_state(db, _key(wid, c, dim)).get("cur", {}), _rows(db, wid, c, col)) for c in cohorts if dim not in owned}
            for c, ev in pooled_decay(per, (now.date() - D0).days).items():
                st = _state(db, _key(wid, c, dim))
                d = {"phase": "done", "kind": ROLLBACK, "dist": {ev["baseline"]: 1.0}, "winner": ev["baseline"], "evidence": ev,
                     "why": f"lift of {ev['winner']} over holdout {ev['baseline']} is gone (pooled z={ev['pooled_z']} over "
                            f"{ev['pooled_holdout_n']} holdout calls, last {RECENT_DAYS + 1}d)", "all": {ev["winner"]: ev}}
                changes = [x for x in changes if not (x[0] == c and x[1] == dim)]
                changes.append((c, dim, _key(wid, c, dim), st, d))
                new_cfg.setdefault("allocations", {}).setdefault(c, {})[dim] = d["dist"]
    if not changes:
        return
    rollback = any(d["kind"] == ROLLBACK for *_, d in changes)
    kind = ROLLBACK if rollback else KIND
    explore_only = all(d["phase"] == "explore" and "start" in d["why"] for *_, d in changes)
    for c, dim, k, st, d in changes:
        st["pending"] = {**{x: d[x] for x in ("phase", "dist", "champion", "winner", "baseline") if x in d}}
        _save(db, k, st)
    ev = {f"{c}:{dim}": {"why": d["why"], "evidence": d.get("evidence"), "arms": d.get("all")} for c, dim, _, _, d in changes}
    gain = sum(d["evidence"]["lift"] * _share(db, wid, c) for c, dim, _, _, d in changes if d.get("evidence") and d["kind"] == KIND)
    gov.propose(
        workflow=wid, kind=kind, base_version=version, base_config=cfg, config=new_cfg,
        summary=("Start welcome/voice experiments: " if explore_only else "Experiment update: ")
                + "; ".join(f"{c}/{dim}: {d['why']}" for c, dim, _, _, d in changes[:4]) + ("…" if len(changes) > 4 else ""),
        diff={f"{c}/{dim}": d["dist"] for c, dim, _, _, d in changes},
        affected_leads=wf["daily_batch"]["size"],
        dry_run=({"method": "observed contemporaneous arm comparison (not a forecast)", "arms": ev} if not explore_only else
                 {"method": "none: exploration. History holds a single W/V variant, so there is nothing to replay",
                  "exposure": f"champion keeps {CHAMPION_SHARE:.0%}, challengers share the rest"}),
        impact=1.0 if rollback else (min(1.0, gain / 0.03) if gain else 0.3),
        rationale=_why(rollback, explore_only),
        inputs={"changes": ev, "thresholds": {"alpha": ALPHA, "looks": LOOKS, "min_n": MIN_N, "min_lift": MIN_LIFT}},
        now=now, priority=1 if rollback else 0)


def _share(db: sqlite3.Connection, wid: str, cohort: str) -> float:
    r = db.execute("SELECT SUM(CASE WHEN cohort=? THEN connected END), SUM(connected) FROM call_agg WHERE workflow=?",
                   (cohort, wid)).fetchone()
    return (r[0] or 0) / r[1] if r[1] else 1.0


def _why(rollback: bool, explore_only: bool) -> str:
    if rollback:
        return "Rollback: the promoted variant no longer beats the holdout kept on the previous champion."
    if explore_only:
        return ("Welcome and voice never varied in history, so the only way to learn their effect is to run them. "
                "Champion keeps a large share; engagement among connected calls is compared across arms.")
    return (f"Chosen by a Holm-corrected one-sided z-test on engagement among connected calls (workflow alpha {ALPHA} over "
            f"{LOOKS} looks and all cohort x dimension tests), lift >= {MIN_LIFT:.0%}, late-half lift retained, pickup guardrail clear. "
            f"A {HOLDOUT_SHARE:.0%} holdout stays on the previous champion to detect decay.")


def _supervisor_edit(db: sqlite3.Connection, gov: Governance, wf: dict, cur: dict, now: datetime) -> bool:
    """A supervisor changed welcome/voice/cohorts: their edit wins. Forget what the experiment learned about the
    touched dimensions (the regime changed), pause for SUPERVISOR_HOLD_H, and, if our per-cohort allocations would
    mask their default-level edit, ask (never force) to remove those overrides so the edit actually takes effect."""
    wid, cfg = wf["workflow_id"], cur["config"]
    r = db.execute("SELECT event_id, ts, body FROM sim_events WHERE type='config_changed' AND workflow=? AND "
                   "body LIKE '%\"actor\":\"supervisor\"%' ORDER BY event_id DESC LIMIT 1", (wid,)).fetchone()
    if not r or str(r[0]) == (db.execute("SELECT value FROM meta WHERE key=?", (f"exp_sup_seen:{wid}",)).fetchone() or [""])[0]:
        return False
    dims = set(json.loads(r[2]).get("changed_dims", [])) & {"welcome_message", "voice", "cohorts"}
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"exp_sup_seen:{wid}", str(r[0])))
    if not dims:
        return False
    owned = set(json.loads((db.execute("SELECT value FROM meta WHERE key=?", (f"exp_owned:{wid}",)).fetchone() or ["[]"])[0]))
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)",   # ownership: we never explore a dimension a human has set
               (f"exp_owned:{wid}", json.dumps(sorted(owned | (dims & {"welcome_message", "voice"})))))
    db.execute("DELETE FROM meta WHERE key LIKE ?", (f"exp:{wid}:%",))
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)",
               (f"exp_hold:{wid}", (now + timedelta(hours=SUPERVISOR_HOLD_H)).isoformat()))
    new_cfg, removed = copy.deepcopy(cfg), []
    for c, al in new_cfg.get("allocations", {}).items():
        for d in dims & {"welcome_message", "voice"}:
            if c != "default" and d in al:
                del al[d]
                removed.append(f"{c}/{d}")
    db.execute("INSERT INTO decisions(ts,workflow,kind,inputs,outcome) VALUES(?,?,?,?,?)",
               (now.isoformat(), wid, KIND, json.dumps({"supervisor_event": r[0], "dims": sorted(dims), "overrides": removed}),
                f"supervisor edited {sorted(dims)}: experiments reset and paused {SUPERVISOR_HOLD_H}h; their edit is not reverted"))
    if removed:
        gov.propose(
            workflow=wid, kind="experiment_reset", base_version=cur["version"], base_config=cfg, config=new_cfg,
            summary=f"Make your {', '.join(sorted(dims))} edit apply to every cohort (remove {len(removed)} experiment overrides)",
            diff={"removed_overrides": removed},
            affected_leads=wf["daily_batch"]["size"],
            dry_run={"method": "none: removes experiment-only per-cohort allocations so the supervisor's default applies",
                     "removed": removed},
            impact=0.8, priority=1, now=now,
            rationale="A supervisor changed the default allocation. Our per-cohort experiment overrides would silently "
                      "mask it for those cohorts, so we ask to remove the overrides. The supervisor's edit itself is untouched "
                      "and experiments pause for 48h.",
            inputs={"supervisor_event": r[0], "removed": removed})
    return True
