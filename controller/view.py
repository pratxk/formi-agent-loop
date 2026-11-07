"""One-screen supervisor view built only from our own store: what is being tested, what is winning, what we need
from you, what changed today, and loop health.  `python -m controller.view [--db PATH] [--json] [--workflow ID]`"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta

from .planners import experiments as ex
from .planners import incidents
from .store import connect, meta_get


def _tests(db: sqlite3.Connection, wid: str) -> list[dict]:
    out = []
    for k, v in db.execute("SELECT key, value FROM meta WHERE key LIKE ?", (f"exp:{wid}:%",)):
        _, _, cohort, dim = k.split(":", 3)
        st = json.loads(v)
        cu = st.get("cur")
        if not cu:
            out.append({"cohort": cohort, "dim": dim, "phase": "waiting for approval"})
            continue
        col = dict(ex.DIMS)[dim]
        rows, row = ex._rows(db, wid, cohort, col), {"cohort": cohort, "dim": dim, "phase": cu["phase"], "allocation": cu["dist"]}
        ref = cu.get("champion") or cu.get("baseline")
        arms = {}
        for o in cu["dist"]:
            if o != ref and (c := ex.compare(rows, o, ref, cu.get("since_version", 0))):
                arms[o] = {"lift_vs_" + ref: round(c["lift"], 4), "z": round(c["z"], 2), "connected": [c["n_a"], c["n_b"]]}
        row["arms"] = arms
        if cu["phase"] == "promoted":
            row["winner"] = cu["winner"]
        out.append(row)
    return out


def build(db: sqlite3.Connection, wid: str | None = None) -> dict:
    sim_ts = db.execute("SELECT MAX(sim_ts) FROM loop_metrics").fetchone()[0]
    now = datetime.fromisoformat(sim_ts) if sim_ts else None
    wfs = [r[0] for r in db.execute("SELECT DISTINCT workflow FROM proposals UNION SELECT DISTINCT workflow FROM call_agg")]
    out: dict = {"as_of": sim_ts, "workflows": {}}
    for w in sorted(wfs):
        if wid and w != wid:
            continue
        since = (now - timedelta(hours=24)).isoformat() if now else ""
        out["workflows"][w] = {
            "being_tested": _tests(db, w),
            "needs_you": [dict(r) for r in db.execute(
                "SELECT proposal_id, kind, state, summary, impact, submitted_ts FROM proposals WHERE workflow=? AND state IN "
                "('QUEUED','SUBMITTED','APPROVED') ORDER BY impact DESC", (w,))],
            "changed_last_24h": [{"ts": r[0], **{k: v for k, v in json.loads(r[1]).items() if k in ("version", "actor", "changed_dims")}}
                                 for r in db.execute("SELECT ts, body FROM sim_events WHERE type='config_changed' AND workflow=? AND ts>? "
                                                     "ORDER BY ts", (w, since))],
            "incident": json.loads(r[0]) if (r := db.execute("SELECT value FROM meta WHERE key=?", (f"inc:{w}",)).fetchone()) else None,
            "prompts": {p: {"connected": v["n"], "flag_rate": round(v["rate"], 4)} for p, v in incidents.prompt_stats(db, w).items()},
        }
    n_dec = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    lm = db.execute("SELECT COUNT(*), AVG(tick_s), MAX(tick_s), SUM(events) FROM loop_metrics").fetchone()
    out["loop_health"] = {"ticks": lm[0], "avg_tick_s": round(lm[1] or 0, 3), "max_tick_s": round(lm[2] or 0, 2),
                          "events_ingested": lm[3], "decisions_logged": n_dec,
                          "ingest_cursor_chunk": meta_get(db, "last_chunk"), "credits_remaining": meta_get(db, "credits")}
    return out


def render(v: dict) -> str:
    L = [f"As of {v['as_of']}", ""]
    for w, d in v["workflows"].items():
        L.append(f"== {w}")
        L.append("Being tested:" if d["being_tested"] else "Being tested: nothing")
        for t in d["being_tested"]:
            best = max(t.get("arms", {}).items(), key=lambda kv: list(kv[1].values())[0], default=None)
            L.append(f"  {t['cohort']}/{t['dim']}: {t['phase']}" + (f"; leading: {best[0]} {list(best[1].values())[0]:+.1%} (z={best[1]['z']})" if best else ""))
        L.append("Needs you:" if d["needs_you"] else "Needs you: nothing")
        for p in d["needs_you"]:
            L.append(f"  [{p['state']}] impact {p['impact']:.2f}: {p['summary'][:110]}")
        L.append("Changed in last 24h: " + (", ".join(f"v{c['version']} by {c['actor']} {c['changed_dims']}" for c in d["changed_last_24h"]) or "nothing"))
        if d["incident"]:
            L.append(f"Incident: {d['incident'].get('phase')} on prompt {d['incident'].get('bad_prompt')}")
        L.append("")
    h = v["loop_health"]
    L.append(f"Loop health: {h['ticks']} ticks, avg {h['avg_tick_s']}s (max {h['max_tick_s']}s), {h['events_ingested']} events, "
             f"{h['decisions_logged']} decisions logged, credits {h['credits_remaining']}")
    return "\n".join(L)


def weekly(db: sqlite3.Connection, tenant_prefix: str = "") -> str:
    """The summary Northwind compliance asked for: every change we made under the signed operational latitude
    (call timing, caller ID) and every approved customer-facing change, with the reason."""
    out = []
    events = db.execute("SELECT ts, body FROM sim_events WHERE type='config_changed' ORDER BY event_id").fetchall()
    db.row_factory = sqlite3.Row
    for ts, body in events:
        b = json.loads(body)
        if b["actor"] == "supervisor" or not b["workflow_id"].startswith(tenant_prefix):
            continue
        pr = db.execute("SELECT summary, rationale, via FROM proposals WHERE proposal_id=? OR approval_id=?",
                        (b.get("proposal_id"), b.get("approval_id"))).fetchone()
        out.append(f"{ts[:16]}  {b['workflow_id']} v{b['version']} [{b['actor']}] {b['changed_dims']}: "
                   + (f"{pr['summary']} -- {pr['rationale'][:140]}" if pr else "(no proposal record)"))
    return "\n".join(out) or "no changes"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("CONTROLLER_DB", "state/controller.db"))
    ap.add_argument("--workflow")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--weekly", metavar="WORKFLOW_PREFIX", nargs="?", const="", help="change log with reasons (client summary)")
    a = ap.parse_args()
    if a.weekly is not None:
        print(weekly(connect(a.db), a.weekly))
        return
    v = build(connect(a.db), a.workflow)
    print(json.dumps(v, indent=2) if a.json else render(v))


if __name__ == "__main__":
    main()
