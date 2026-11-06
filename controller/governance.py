"""Governance client: proposal lifecycle, batching, timing, auto-apply routing, crash reconciliation.

Every state change is written to SQLite *before* the next external side effect. Crash windows are the two
transient states SUBMITTING / APPLYING; `recover()` resolves them from the simulator's own approval list.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import configdiff as cd
from .api import Sim

log = logging.getLogger("controller.gov")

OPERATIONAL = {"schedule", "caller_id"}  # dims we are willing to auto-apply even where policy allows more
ACTIVE = ("QUEUED", "BATCHED", "SUBMITTING", "SUBMITTED", "APPROVED", "APPLYING")
RESERVED_SLOTS = 1  # of max_requests_per_24h, kept for rollbacks / incident fixes


@dataclass(frozen=True)
class Policy:
    workflow: str
    tenant: str
    hours: tuple[int, int]
    lat_max: int
    ttl_h: int
    max_req: int
    auto_enabled: bool
    auto_dims: frozenset[str]
    auto_shift: float
    min_pass: float
    mid_flight: str
    primary: str
    guardrail: dict

    @staticmethod
    def of(w: dict) -> "Policy":
        g, a = w["governance"], w["governance"]["auto_apply"]
        return Policy(w["workflow_id"], w["tenant_id"], tuple(g["approver_hours"]), g["approver_latency_minutes"][1],
                      g["proposal_ttl_hours"], g["max_requests_per_24h"], a["enabled"], frozenset(a["allowed_dims"]),
                      a["max_shift"], g["min_validation_pass_rate"], w["mid_flight"], w["objective"]["primary"],
                      w["objective"]["guardrail"])


def load_prefs(path: str = "config/client_prefs.json") -> dict[str, dict]:
    p = Path(path)
    return {k: v for k, v in json.loads(p.read_text()).items() if not k.startswith("_")} if p.exists() else {}


def _j(x: Any) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


class Governance:
    def __init__(self, db: sqlite3.Connection, sim: Sim, policies: dict[str, Policy],
                 prefs: dict[str, dict] | None = None) -> None:
        self.db, self.sim, self.pol, self.prefs = db, sim, policies, prefs or {}
        db.row_factory = sqlite3.Row

    # ---------- state helpers ----------
    def _ts(self, now: datetime) -> str:
        return now.isoformat()

    def _set(self, pid: str, state: str, now: datetime, note: str = "", **cols: Any) -> None:
        row = self.db.execute("SELECT state FROM proposals WHERE proposal_id=?", (pid,)).fetchone()
        sets = ", ".join(["state=?"] + [f"{k}=?" for k in cols])
        self.db.execute("BEGIN")
        self.db.execute(f"UPDATE proposals SET {sets} WHERE proposal_id=?", (state, *cols.values(), pid))
        self.db.execute("INSERT INTO proposal_log(ts,proposal_id,from_state,to_state,note) VALUES(?,?,?,?,?)",
                        (self._ts(now), pid, row["state"] if row else None, state, note))
        for m in self._members(pid):  # batch members follow their batch's fate
            if state in ("APPLIED", "REJECTED", "EXPIRED", "LAPSED", "STALE"):
                self.db.execute("UPDATE proposals SET state=? WHERE proposal_id=?", (state, m))
                self.db.execute("INSERT INTO proposal_log(ts,proposal_id,from_state,to_state,note) VALUES(?,?,?,?,?)",
                                (self._ts(now), m, "BATCHED", state, f"via batch {pid}"))
        self.db.execute("COMMIT")

    def _members(self, pid: str) -> list[str]:
        r = self.db.execute("SELECT replaces FROM proposals WHERE proposal_id=?", (pid,)).fetchone()
        return json.loads(r["replaces"]) if r and r["replaces"] else []

    # ---------- planner-facing API ----------
    def propose(self, *, workflow: str, kind: str, base_version: int, base_config: dict, config: dict,
                summary: str, diff: dict, affected_leads: int, dry_run: dict, impact: float, rationale: str,
                inputs: dict, now: datetime, evidence: dict | None = None, priority: int = 0) -> str | None:
        """Queue a change. Idempotent (deterministic id). Returns proposal_id, or None if nothing to do."""
        dims = cd.changed_dims(base_config, config)
        if not dims:
            return None
        pid = hashlib.sha1(f"{workflow}|{base_version}|{kind}|{_j(config)}".encode()).hexdigest()[:16]
        if self.db.execute("SELECT 1 FROM proposals WHERE proposal_id=?", (pid,)).fetchone():
            return pid
        pol = self.pol[workflow]
        auto_ok = (not (dims - pol.auto_dims) and pol.auto_enabled and
                   cd.max_shift(base_config, config) <= pol.auto_shift * 0.95 and
                   (dims <= OPERATIONAL or kind == "rollback"))
        via = "auto" if auto_ok else "approval"
        # one in-flight change per (workflow, kind): newer queued one replaces an older queued one
        for old in self.db.execute("SELECT proposal_id FROM proposals WHERE workflow=? AND kind=? AND state='QUEUED'",
                                   (workflow, kind)).fetchall():
            self._set(old["proposal_id"], "SUPERSEDED", now, f"replaced by {pid}")
        self.db.execute("BEGIN")
        self.db.execute(
            "INSERT INTO proposals(proposal_id,workflow,kind,state,base_version,priority,config,dims,summary,diff,"
            "affected_leads,dry_run,impact,rationale,evidence,via,created_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, workflow, kind, "QUEUED", base_version, priority, _j(config), _j(sorted(dims)), summary, _j(diff),
             affected_leads, _j(dry_run), max(0.0, min(1.0, impact)), rationale, _j(evidence) if evidence else None,
             via, self._ts(now)))
        self.db.execute("INSERT INTO proposal_log(ts,proposal_id,from_state,to_state,note) VALUES(?,?,?,?,?)",
                        (self._ts(now), pid, None, "QUEUED", f"via={via} dims={sorted(dims)}"))
        self.db.execute("INSERT INTO decisions(ts,workflow,kind,inputs,outcome,proposal_id) VALUES(?,?,?,?,?,?)",
                        (self._ts(now), workflow, kind, _j(inputs), rationale, pid))
        self.db.execute("COMMIT")
        return pid

    def active(self, workflow: str, kind: str | None = None) -> list[sqlite3.Row]:
        q = f"SELECT * FROM proposals WHERE workflow=? AND state IN ({','.join('?' * len(ACTIVE))})"
        a: list[Any] = [workflow, *ACTIVE]
        if kind:
            q, a = q + " AND kind=?", [*a, kind]
        return self.db.execute(q, a).fetchall()

    # ---------- the per-tick driver ----------
    def tick(self, now: datetime) -> None:
        self.recover(now)
        self._poll(now)
        for wf in self.pol:
            cur = self.sim.get(f"/workflows/{wf}/config")
            self._expire_stale(wf, cur["version"], now)
            self._release_auto(wf, cur, now)
            self._release_approvals(wf, cur, now)

    # ---------- crash recovery ----------
    def recover(self, now: datetime) -> None:
        for r in self.db.execute("SELECT * FROM proposals WHERE state IN ('SUBMITTING','APPLYING')").fetchall():
            ap = self._find(r["workflow"], r["proposal_id"])
            if r["state"] == "SUBMITTING":
                if ap:
                    self._set(r["proposal_id"], "SUBMITTED", now, "recovered: sim has it", approval_id=ap["approval_id"],
                              submitted_ts=ap["created_at"])
                else:
                    self._set(r["proposal_id"], "QUEUED", now, "recovered: never reached sim")
            elif r["via"] == "auto":  # auto PUT has no approval record; the config history is the witness
                hist = self.sim.get(f"/workflows/{r['workflow']}/config/history")
                hit = next((h for h in hist if h.get("proposal_id") == r["proposal_id"]), None)
                self._set(r["proposal_id"], "APPLIED" if hit else "QUEUED", now, "recovered from config history",
                          applied_version=hit["version"] if hit else None)
            elif ap:  # APPLYING via approval: did the apply land?
                st = ap["status"]
                self._set(r["proposal_id"], "APPLIED" if st == "applied" else "APPROVED", now, f"recovered: sim says {st}")

    def _find(self, wf: str, pid: str) -> dict | None:
        for ap in self.sim.get("/approvals", params={"workflow_id": wf}):
            if ap.get("proposal_id") == pid:
                return ap
        return None

    # ---------- approval polling / apply ----------
    def _poll(self, now: datetime) -> None:
        for r in self.db.execute("SELECT * FROM proposals WHERE state IN ('SUBMITTED','APPROVED')").fetchall():
            if r["state"] == "SUBMITTED":
                ap = self.sim.get(f"/approvals/{r['approval_id']}")
                st = ap["status"]
                if st == "approved":
                    self._set(r["proposal_id"], "APPROVED", now, "approver said yes", decided_ts=self._ts(now),
                              apply_by=ap.get("apply_by"))
                elif st in ("rejected", "expired"):
                    self._set(r["proposal_id"], st.upper(), now, f"{st}: {ap.get('reasons')}", decided_ts=self._ts(now),
                              reasons=_j(ap.get("reasons")))
                    continue
                else:
                    continue
            self._apply(self.db.execute("SELECT * FROM proposals WHERE proposal_id=?", (r["proposal_id"],)).fetchone(), now)

    def _apply(self, r: sqlite3.Row, now: datetime) -> None:
        pid = r["proposal_id"]
        self._set(pid, "APPLYING", now, "apply")
        status, body = self.sim.call("POST", f"/workflows/{r['workflow']}/apply", json={"approval_id": r["approval_id"]})
        if status == 200:
            self._set(pid, "APPLIED", now, "applied", applied_ts=self._ts(now), applied_version=body.get("version"))
        elif status == 410:
            self._set(pid, "LAPSED", now, "apply window missed")
        elif status == 409 and body.get("error") == "stale_base_version":
            self._set(pid, "STALE", now, "base moved before apply (supervisor edit?)")
        else:
            self._set(pid, "APPROVED", now, f"apply retry later: {status} {body}")

    # ---------- release ----------
    def _expire_stale(self, wf: str, version: int, now: datetime) -> None:
        for r in self.db.execute("SELECT proposal_id, base_version FROM proposals WHERE workflow=? AND state='QUEUED'",
                                 (wf,)).fetchall():
            if r["base_version"] < version:
                self._set(r["proposal_id"], "SUPERSEDED", now, f"base v{r['base_version']} < current v{version}")

    def _release_auto(self, wf: str, cur: dict, now: datetime) -> None:
        for r in self.db.execute("SELECT * FROM proposals WHERE workflow=? AND state='QUEUED' AND via='auto'",
                                 (wf,)).fetchall():
            self._set(r["proposal_id"], "APPLYING", now, "auto-apply")
            status, body = self.sim.call("PUT", f"/workflows/{wf}/config", json={
                "proposal_id": r["proposal_id"], "base_version": r["base_version"],
                "proposed_config": json.loads(r["config"]), "rationale": r["rationale"]})
            if status == 200:
                self._set(r["proposal_id"], "APPLIED", now, "auto-applied", applied_ts=self._ts(now),
                          applied_version=body.get("version"))
            elif status == 403:  # our pre-check disagreed with the sim: fall back to the approval path
                self.db.execute("UPDATE proposals SET via='approval' WHERE proposal_id=?", (r["proposal_id"],))
                self._set(r["proposal_id"], "QUEUED", now, f"auto refused {body}; using approval path")
            elif status == 409:
                self._set(r["proposal_id"], "SUPERSEDED", now, f"auto-apply conflict {body}")
            else:
                self._set(r["proposal_id"], "QUEUED", now, f"auto-apply retry: {status}")

    def _can_submit_now(self, pol: Policy, now: datetime) -> bool:
        s, e = pol.hours
        pref = self.prefs.get(pol.tenant, {})
        s = max(s, pref.get("earliest_hour", s))
        h = now.hour + now.minute / 60
        if not (s <= h < e):
            return False
        return h + pol.lat_max / 60 <= e or h - s < 0.25  # decision lands same day, or first slot of the day

    def _quota_left(self, pol: Policy, now: datetime, priority: int) -> bool:
        since = self._ts(now - timedelta(hours=24))
        used = self.db.execute("SELECT COUNT(*) FROM proposals WHERE workflow=? AND submitted_ts>?",
                               (pol.workflow, since)).fetchone()[0]
        cap = min(pol.max_req, self.prefs.get(pol.tenant, {}).get("max_per_24h", pol.max_req))
        return used < cap - (0 if priority > 0 else RESERVED_SLOTS if cap > 1 else 0)

    def _release_approvals(self, wf: str, cur: dict, now: datetime) -> None:
        pol = self.pol[wf]
        queued = self.db.execute("SELECT * FROM proposals WHERE workflow=? AND state='QUEUED' AND via='approval' "
                                 "ORDER BY priority DESC, impact DESC", (wf,)).fetchall()
        in_flight = self.db.execute("SELECT 1 FROM proposals WHERE workflow=? AND via='approval' AND state IN "
                                    "('SUBMITTING','SUBMITTED','APPROVED','APPLYING')", (wf,)).fetchone()
        if not queued or in_flight:
            return  # one outstanding request per workflow keeps the approver's queue short
        if not (self._can_submit_now(pol, now) and self._quota_left(pol, now, max(q["priority"] for q in queued))):
            return
        batch = self._batch(queued, cur)
        self._submit(batch, cur, now)

    def _batch(self, queued: list[sqlite3.Row], cur: dict) -> dict:
        """Merge compatible queued proposals into one request (disjoint dims, at most one prompt change)."""
        taken: set[str] = set()
        parts, members = [], []
        for q in queued:
            d = set(json.loads(q["dims"]))
            if d & taken:
                continue
            taken |= d
            parts.append((json.loads(q["config"]), d))
            members.append(q)
        if len(members) == 1:
            m = members[0]
            return {"row": m, "config": json.loads(m["config"]), "members": [], "evidence": json.loads(m["evidence"] or "null")}
        ev = next((json.loads(m["evidence"]) for m in members if m["evidence"]), None)
        return {"row": None, "members": members, "config": cd.merge(cur["config"], parts), "evidence": ev}

    def _submit(self, batch: dict, cur: dict, now: datetime) -> None:
        wf_row = batch["row"] or batch["members"][0]
        wf = wf_row["workflow"]
        if batch["row"] is None:
            ms = batch["members"]
            pid = hashlib.sha1("|".join(sorted(m["proposal_id"] for m in ms)).encode()).hexdigest()[:16]
            self.db.execute(
                "INSERT OR IGNORE INTO proposals(proposal_id,workflow,kind,state,base_version,priority,config,dims,summary,diff,"
                "affected_leads,dry_run,impact,rationale,evidence,via,created_ts,replaces) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, wf, "batch", "QUEUED", cur["version"], max(m["priority"] for m in ms), _j(batch["config"]),
                 _j(sorted(set().union(*[json.loads(m["dims"]) for m in ms]))), " + ".join(m["summary"] for m in ms),
                 _j({m["proposal_id"]: json.loads(m["diff"]) for m in ms}), max(m["affected_leads"] for m in ms),
                 _j({m["proposal_id"]: json.loads(m["dry_run"]) for m in ms}), max(m["impact"] for m in ms),
                 " | ".join(m["rationale"] for m in ms), _j(batch["evidence"]) if batch["evidence"] else None,
                 "approval", self._ts(now), _j([m["proposal_id"] for m in ms])))
            for m in ms:
                self._set(m["proposal_id"], "BATCHED", now, f"into {pid}")
        else:
            pid = wf_row["proposal_id"]
        r = self.db.execute("SELECT * FROM proposals WHERE proposal_id=?", (pid,)).fetchone()
        if not self._valid(r):
            self._set(pid, "SUPERSEDED", now, "failed local validation (would be rejected by approver)")
            return
        self._set(pid, "SUBMITTING", now, "POST /approvals")
        status, body = self.sim.call("POST", "/approvals", json={
            "proposal_id": pid, "workflow_id": wf, "base_version": r["base_version"], "proposed_config": batch["config"],
            "change_summary": r["summary"], "diff": json.loads(r["diff"]), "affected_leads": r["affected_leads"],
            "dry_run": json.loads(r["dry_run"]), "impact_score": r["impact"], "rationale": r["rationale"],
            **({"evidence": batch["evidence"]} if batch["evidence"] else {})})
        if status == 200:
            self._set(pid, "SUBMITTED", now, "pending approver", approval_id=body["approval_id"], submitted_ts=self._ts(now))
        elif status == 409 and body.get("error") == "duplicate_proposal_id":
            ap = self._find(wf, pid)
            self._set(pid, "SUBMITTED", now, "adopted existing", approval_id=ap["approval_id"] if ap else None,
                      submitted_ts=self._ts(now))
        elif status == 409:  # stale_base_version
            self._set(pid, "STALE", now, f"{body}")
        else:  # 4xx quota etc.: keep queued, retry next tick
            self._set(pid, "QUEUED", now, f"submit refused {status} {body}")

    @staticmethod
    def _valid(r: sqlite3.Row) -> bool:
        ev = json.loads(r["evidence"] or "null")
        needs_ev = "prompt_version" in json.loads(r["dims"])
        return bool(r["summary"] and r["rationale"] and json.loads(r["diff"]) and json.loads(r["dry_run"])
                    and 0 <= r["impact"] <= 1 and r["affected_leads"] >= 0 and (not needs_ev or (ev and ev.get("validation_run_id"))))
