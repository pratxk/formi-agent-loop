"""Audit query: why did workflow X change at time T, who approved it, what happened next?
Usage: python -m controller.why <workflow_id> <ISO-ts> [--db state/controller.db]"""
from __future__ import annotations

import argparse
import json
import sqlite3


def _kpi(db: sqlite3.Connection, wf: str, version: int) -> str:
    n, c, e = db.execute("SELECT COUNT(*), SUM(connected), SUM(engaged) FROM call_audit WHERE workflow=? AND version=?",
                         (wf, version)).fetchone()
    if not n:
        return "no calls yet"
    return f"{n} dials, pickup {c / n:.1%}, engagement {(e / c if c else 0):.1%} (of {c} connected)"


def why(db: sqlite3.Connection, wf: str, ts: str) -> str:
    db.row_factory = sqlite3.Row
    ev = db.execute("SELECT * FROM sim_events WHERE type='config_changed' AND workflow=? AND ts<=? "
                    "ORDER BY event_id DESC LIMIT 1", (wf, ts)).fetchone()
    if not ev:
        return f"No config change for {wf} at or before {ts}."
    b = json.loads(ev["body"])
    out = [f"{wf} -> config v{b['version']} at {ev['ts']} by {b['actor']}; dims={b['changed_dims']}"]
    if b["actor"] == "supervisor":
        out.append("  Client supervisor edit (bypasses governance). We did not make or approve it.")
    pid = b.get("proposal_id")
    pr = db.execute("SELECT * FROM proposals WHERE proposal_id=?", (pid,)).fetchone() if pid else None
    if pr is None and b.get("approval_id"):
        pr = db.execute("SELECT * FROM proposals WHERE approval_id=?", (b["approval_id"],)).fetchone()
    if pr:
        out += [f"  Why: {pr['summary']}", f"  Rationale: {pr['rationale']}",
                f"  Impact score {pr['impact']:.2f}, affected leads {pr['affected_leads']}, route={pr['via']}, kind={pr['kind']}"]
        if pr["evidence"]:
            out.append(f"  Evidence: {pr['evidence']}")
        dec = db.execute("SELECT * FROM decisions WHERE proposal_id=?", (pr["proposal_id"],)).fetchone()
        if dec:
            out.append(f"  Decision inputs @ {dec['ts']}: {dec['inputs']}")
        out.append("  Lifecycle:")
        for r in db.execute("SELECT ts, from_state, to_state, note FROM proposal_log WHERE proposal_id=? ORDER BY id",
                            (pr["proposal_id"],)):
            out.append(f"    {r['ts']}  {r['from_state'] or '-'} -> {r['to_state']}  {r['note']}")
        if pr["approval_id"]:
            out.append(f"  Approver decision: approval {pr['approval_id']}, reasons={pr['reasons']}")
    elif b["actor"] != "supervisor":
        out.append("  No matching proposal in our store (recorded by sim only).")
    out.append(f"  Before (v{b['version'] - 1}): {_kpi(db, wf, b['version'] - 1)}")
    out.append(f"  After  (v{b['version']}): {_kpi(db, wf, b['version'])}")
    nxt = db.execute("SELECT ts, body FROM sim_events WHERE type='config_changed' AND workflow=? AND event_id>? "
                     "ORDER BY event_id LIMIT 1", (wf, ev["event_id"])).fetchone()
    if nxt:
        nb = json.loads(nxt["body"])
        out.append(f"  Next change: v{nb['version']} at {nxt['ts']} by {nb['actor']} dims={nb['changed_dims']}")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("workflow")
    ap.add_argument("ts")
    ap.add_argument("--db", default="state/controller.db")
    a = ap.parse_args()
    print(why(sqlite3.connect(a.db), a.workflow, a.ts))
