"""Hallucination incidents: detect on rates, pick a safe replacement prompt, ask a human, verify afterwards.

We never overwrite a supervisor's prompt on our own: the fix is a proposal through governance (prompt changes
are never auto-applied) with a validation run as evidence. The validation run measures safety only, so
engagement is judged from calls we actually observed, and near-verbatim scripts are excluded up front (the
"read the compliance script word for word" failure: clean safety, nobody stays on the line).

State (`meta` key `inc:<workflow>`) is written after every credit-spending call, so a restart never pays twice
for the same validation. Phases: validating -> proposed -> verifying -> resolved | failed."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

from .. import stats
from ..api import Sim
from ..governance import Governance, Policy

KIND = "incident_fix"
MIN_CUR_N, MIN_BASE_N = 150, 300          # connected calls under the suspect prompt / under the reference prompts
Z_BAD, RATIO_BAD, ABS_BAD = 3.0, 2.0, 0.04  # act when z>3 and >=2x reference (or >4% if no reference exists)
VALIDATE_N, MAX_VALIDATIONS, CREDIT_RESERVE = 200, 3, 20
VERIFY_N, ENG_GUARD = 200, 0.02
VERBATIM = re.compile(r"verbatim|word[- ]for[- ]word|compliance script|\bstrict\b", re.I)


def _key(wid: str) -> str:
    return f"inc:{wid}"


def _load(db: sqlite3.Connection, wid: str) -> dict:
    r = db.execute("SELECT value FROM meta WHERE key=?", (_key(wid),)).fetchone()
    return json.loads(r[0]) if r else {}


def _save(db: sqlite3.Connection, wid: str, v: dict) -> None:
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (_key(wid), json.dumps(v, sort_keys=True)))


def prompt_stats(db: sqlite3.Connection, wid: str) -> dict[str, dict]:
    out = {}
    for p, con, eng, hal in db.execute(
            "SELECT prompt, SUM(connected), SUM(engaged), SUM(halluc) FROM call_agg WHERE workflow=? GROUP BY 1", (wid,)):
        out[p] = {"n": con or 0, "eng": (eng or 0) / con if con else None, "hal": hal or 0,
                  "rate": (hal or 0) / con if con else 0.0}
    return out


def detect(ps: dict[str, dict], cur: str) -> dict | None:
    """Is the prompt now live flagged far more often than the other prompts we have observed?"""
    c = ps.get(cur)
    if not c or c["n"] < MIN_CUR_N:
        return None
    rn = sum(v["n"] for p, v in ps.items() if p != cur)
    rh = sum(v["hal"] for p, v in ps.items() if p != cur)
    if rn >= MIN_BASE_N:
        z = stats.two_prop_z(c["hal"], c["n"], rh, rn)
        ref = rh / rn
        if z > Z_BAD and c["rate"] >= RATIO_BAD * max(ref, 0.002):
            return {"rate": c["rate"], "ref_rate": ref, "z": round(z, 2), "n": c["n"], "ref_n": rn}
        return None
    if c["rate"] > ABS_BAD:
        return {"rate": c["rate"], "ref_rate": None, "z": None, "n": c["n"], "ref_n": rn}
    return None


def _credits(sim: Sim) -> float:
    return float(sim.get("/sim/state")["credits_remaining"])


def _gather(db: sqlite3.Connection, sim: Sim, wid: str, inc: dict, options: list[str], cur: str, ps: dict) -> bool:
    """Spend credits (once) to learn what each alternative is and whether it passes validation."""
    if "summaries" not in inc:
        if _credits(sim) < CREDIT_RESERVE + 3:
            return False
        st, body = sim.call("POST", "/llm/suggest-fixes", json={
            "workflow_id": wid, "problem": f"Hallucination flag rate on prompt {cur} is {inc['detect']['rate']:.1%}, "
                                           f"well above other prompts; need a safer prompt that keeps callers engaged"})
        inc["summaries"] = {s["prompt_version"]: s["summary"] for s in body.get("suggestions", [])} if st == 200 else {}
        _save(db, wid, inc)
    # observed-in-production prompts first (we know their engagement), then the rest in option order
    def order(p: str) -> tuple:
        o = ps.get(p, {})
        return (0 if o.get("n", 0) >= MIN_BASE_N and o["rate"] < inc["detect"]["rate"] / 2 else 1, options.index(p))
    pool = [p for p in sorted((o for o in options if o != cur), key=order)
            if not VERBATIM.search(inc["summaries"].get(p, ""))]
    for p in pool:
        if len(inc["validations"]) >= MAX_VALIDATIONS or p in inc["validations"]:
            continue
        if _credits(sim) < CREDIT_RESERVE + 2:
            break
        st, body = sim.call("POST", "/validation/runs", json={"workflow_id": wid, "prompt_version": p, "n": VALIDATE_N})
        if st == 200:
            inc["validations"][p] = {"run": body["validation_run_id"], "pass_rate": body["pass_rate"]}
            _save(db, wid, inc)
    return True


def _choose(inc: dict, ps: dict, pol: Policy, tried: list[str]) -> str | None:
    """Best validated prompt: passes safety, not already tried, and (if observed) engagement not clearly worse."""
    ok = [p for p, v in inc["validations"].items() if v["pass_rate"] >= pol.min_pass and p not in tried]
    if not ok:
        return None
    best_eng = max((ps[p]["eng"] for p in ok if ps.get(p, {}).get("n", 0) >= MIN_BASE_N and ps[p]["eng"] is not None), default=None)

    def score(p: str) -> tuple:
        o = ps.get(p, {})
        known = o.get("n", 0) >= MIN_BASE_N
        bad_eng = known and best_eng is not None and o["eng"] < best_eng - ENG_GUARD
        return (not bad_eng, known, inc["validations"][p]["pass_rate"])
    return max(ok, key=score)


def plan(db: sqlite3.Connection, gov: Governance, wf: dict, pol: Policy, cur: dict, now: datetime) -> None:
    wid, cfg = wf["workflow_id"], cur["config"]
    live = cfg.get("prompt_version")
    ps = prompt_stats(db, wid)
    inc = _load(db, wid)
    if inc.get("phase") == "proposed" and live == inc.get("candidate"):
        c0 = ps.get(live, {})
        others = [v for p, v in ps.items() if p not in (live, inc.get("bad_prompt")) and v["n"]]
        ref_n = sum(v["n"] for v in others)
        pre_eng = sum(v["eng"] * v["n"] for v in others) / ref_n if ref_n >= MIN_BASE_N else None
        if pre_eng is None and c0.get("n", 0) >= MIN_BASE_N:  # reverting to a prompt we ran before: its own history is the reference
            pre_eng = c0["eng"]
        inc.update(phase="verifying", verify_from=c0.get("n", 0), applied_at=now.isoformat(),
                   snap={"n": c0.get("n", 0), "eng": round((c0.get("eng") or 0) * c0.get("n", 0)), "hal": c0.get("hal", 0)},
                   pre_eng=pre_eng)
        _save(db, wid, inc)
    if inc.get("phase") == "verifying":
        _verify(db, wid, inc, live, ps, now)
        inc = _load(db, wid)
    if gov.active(wid, KIND):
        return
    det = detect(ps, live)
    if not det:
        return
    if inc.get("phase") in (None, "resolved", "failed") or inc.get("bad_prompt") != live:
        inc = {"phase": "validating", "bad_prompt": live, "detect": det, "validations": {}, "tried": inc.get("tried", []) + ([inc["bad_prompt"]] if inc.get("bad_prompt") not in (None, live) else []),
               "started": now.isoformat()}
        _save(db, wid, inc)
        db.execute("INSERT INTO decisions(ts,workflow,kind,inputs,outcome) VALUES(?,?,?,?,?)",
                   (now.isoformat(), wid, KIND, json.dumps({"prompt": live, **det}),
                    f"hallucination incident: prompt {live} flagged {det['rate']:.1%} vs reference "
                    f"{(det['ref_rate'] or 0):.1%} (z={det['z']})"))
    options = wf["options"]["prompt_version"]
    if not _gather(db, gov.sim, wid, inc, options, live, ps):
        return
    cand = _choose(inc, ps, pol, inc["tried"])
    if cand is None:
        return
    new_cfg = {**cfg, "prompt_version": cand}
    v = inc["validations"][cand]
    base = ps.get(cand, {})
    pid = gov.propose(
        workflow=wid, kind=KIND, base_version=cur["version"], base_config=cfg, config=new_cfg,
        summary=f"Safety fix: replace prompt {live} with {cand} (hallucination flags {det['rate']:.1%} vs "
                f"{(det['ref_rate'] or 0):.1%} on other prompts)",
        diff={"prompt_version": {"from": live, "to": cand}},
        affected_leads=wf["daily_batch"]["size"],
        dry_run={"method": "validation run (synthetic conversations; measures safety only, not engagement)",
                 "candidate": cand, "pass_rate": v["pass_rate"], "run": v["run"], "min_required": pol.min_pass,
                 "observed_in_production": {"n_connected": base.get("n", 0), "engagement": base.get("eng"),
                                            "flag_rate": base.get("rate")},
                 "current": {"prompt": live, "flag_rate": det["rate"], "n_connected": det["n"]}},
        impact=1.0, priority=1, now=now, evidence={"validation_run_id": v["run"]},
        rationale=(f"Automated QA flags {det['rate']:.1%} of connected calls under {live} vs {(det['ref_rate'] or 0):.1%} under "
                   f"other prompts (z={det['z']}, n={det['n']}). {cand} passed validation at {v['pass_rate']:.1%}"
                   f"{' and has been observed in production with engagement ' + format(base['eng'], '.1%') if base.get('eng') else ''}. "
                   f"Near-verbatim scripts were excluded. This replaces a supervisor edit, so it needs approval; nothing is applied silently."),
        inputs={"detect": det, "validations": inc["validations"], "excluded_verbatim": [p for p, s in inc["summaries"].items() if VERBATIM.search(s)],
                "tried": inc["tried"]})
    if pid:
        inc.update(phase="proposed", candidate=cand, proposal=pid)
        _save(db, wid, inc)


def _verify(db: sqlite3.Connection, wid: str, inc: dict, live: str, ps: dict, now: datetime) -> None:
    """Judge the fix on calls made *after* it went live: flag rate must fall, and engagement must not collapse
    (compared with what the non-offending prompts achieved, not with the offender: an aggressive script can
    'engage' more while hallucinating). Failure marks the prompt as tried so the next candidate is used."""
    cand = inc["candidate"]
    if live != cand:
        return
    c, s0 = ps.get(cand, {}), inc["snap"]
    dn = c.get("n", 0) - s0["n"]
    if dn < VERIFY_N:
        return
    rate = (c["hal"] - s0["hal"]) / dn
    eng = (round(c["eng"] * c["n"]) - s0["eng"]) / dn
    bad = ps.get(inc["bad_prompt"], {})
    gone = rate <= max(bad.get("rate", 0) / 2, 0.01)
    # fail on engagement only when we are confident it dropped past the guard (200 calls have a ~3pp standard error)
    eng_ok = inc.get("pre_eng") is None or eng + 1.645 * (eng * (1 - eng) / dn) ** 0.5 >= inc["pre_eng"] - ENG_GUARD
    inc["phase"] = "resolved" if gone and eng_ok else "failed"
    inc["tried"] = inc["tried"] + [cand]
    inc["result"] = {"flag_rate": round(rate, 4), "engagement": round(eng, 4), "before_flag_rate": bad.get("rate"),
                     "reference_engagement": inc.get("pre_eng"), "calls_judged": dn}
    _save(db, wid, inc)
    db.execute("INSERT INTO decisions(ts,workflow,kind,inputs,outcome) VALUES(?,?,?,?,?)",
               (now.isoformat(), wid, KIND, json.dumps(inc["result"]),
                f"incident {inc['phase']}: {cand} flag rate {rate:.1%}, engagement {eng:.1%} over {dn} calls since it went live"))
