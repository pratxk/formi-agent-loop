"""Governance state machine against a fake simulator (no HTTP)."""
import copy
import sqlite3
from datetime import datetime, timedelta

import pytest

from controller import configdiff as cd
from controller.governance import Governance, Policy
from controller.store import connect

BASE = {"cohorts": [], "allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1},
                                                    "caller_id": {"D1": .5, "D2": .5}}},
        "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}
POL = Policy("w1", "t", (10, 19), 120, 24, 3, True, frozenset({"schedule", "caller_id"}), 0.5, 0.97,
             "allowed", "engagement_rate", {})


def at(h, m=0, d=4):
    return datetime(2027, 1, d, h, m)


class FakeSim:
    def __init__(self):
        self.version, self.config, self.approvals, self.posts, self.apply_status = 1, copy.deepcopy(BASE), {}, 0, 200

    def get(self, path, params=None):
        if path.endswith("/config/history"):
            return []
        if path.endswith("/config"):
            return {"version": self.version, "config": self.config}
        if path == "/approvals":
            return [a for a in self.approvals.values() if a["workflow_id"] == params["workflow_id"]]
        return self.approvals[path.split("/")[-1]]

    def call(self, method, path, json=None, retry=True):
        if path == "/approvals":
            if any(a["proposal_id"] == json["proposal_id"] for a in self.approvals.values()):
                return 409, {"error": "duplicate_proposal_id"}
            self.posts += 1
            aid = f"apr_{len(self.approvals) + 1}"
            self.approvals[aid] = {"approval_id": aid, "proposal_id": json["proposal_id"], "workflow_id": "w1",
                                   "status": "pending", "created_at": "x", "cfg": json["proposed_config"]}
            return 200, {"approval_id": aid}
        if path.endswith("/apply"):
            if self.apply_status != 200:
                return self.apply_status, {"error": "stale_base_version" if self.apply_status == 409 else "lapsed"}
            a = self.approvals[json["approval_id"]]
            a["status"], self.config, self.version = "applied", a["cfg"], self.version + 1
            return 200, {"version": self.version}
        self.config, self.version = json["proposed_config"], self.version + 1  # PUT auto-apply
        return 200, {"version": self.version}


@pytest.fixture
def env():
    db, sim = connect(":memory:"), FakeSim()
    return Governance(db, sim, {"w1": POL}), sim, db


def cfg(**dims):
    c = copy.deepcopy(BASE)
    if "voice" in dims:
        c["allocations"]["default"]["voice"] = dims["voice"]
    if "welcome" in dims:
        c["allocations"]["default"]["welcome_message"] = dims["welcome"]
    if "caller_id" in dims:
        c["allocations"]["default"]["caller_id"] = dims["caller_id"]
    return c


def prop(g, now, config, kind="exp", priority=0, **kw):
    return g.propose(workflow="w1", kind=kind, base_version=1, base_config=BASE, config=config, summary="s",
                     diff={"d": 1}, affected_leads=10, dry_run={"r": 1}, impact=.4, rationale="r", inputs={"i": 1},
                     now=now, priority=priority, **kw)


def state(db, pid):
    return db.execute("SELECT state FROM proposals WHERE proposal_id=?", (pid,)).fetchone()[0]


def test_full_approval_path(env):
    g, sim, db = env
    pid = prop(g, at(11), cfg(voice={"V1": .5, "V2": .5}))
    g.tick(at(11))
    assert state(db, pid) == "SUBMITTED"
    next(iter(sim.approvals.values()))["status"] = "approved"
    g.tick(at(11, 15))
    assert state(db, pid) == "APPLIED" and sim.version == 2


def test_waits_for_approver_hours(env):
    g, sim, db = env
    pid = prop(g, at(22), cfg(voice={"V1": .5, "V2": .5}))
    g.tick(at(22))
    assert state(db, pid) == "QUEUED" and sim.posts == 0
    g.tick(at(10, 0, d=5))
    assert sim.posts == 1


def test_operational_dim_auto_applies_anytime(env):
    g, sim, db = env
    pid = prop(g, at(3), cfg(caller_id={"D1": .7, "D2": .3}), kind="callerid")
    g.tick(at(3))
    assert state(db, pid) == "APPLIED" and sim.posts == 0


def test_customer_facing_never_auto(env):
    g, sim, db = env
    pid = prop(g, at(3), cfg(voice={"V1": .9, "V2": .1}))
    assert db.execute("SELECT via FROM proposals WHERE proposal_id=?", (pid,)).fetchone()[0] == "approval"


def test_propose_is_idempotent(env):
    g, _, db = env
    a = prop(g, at(11), cfg(voice={"V1": .5, "V2": .5}))
    b = prop(g, at(11), cfg(voice={"V1": .5, "V2": .5}))
    assert a == b and db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 1


def test_crash_between_post_and_record_adopts_not_duplicates(env):
    g, sim, db = env
    pid = prop(g, at(11), cfg(voice={"V1": .5, "V2": .5}))
    g.tick(at(11))
    db.execute("UPDATE proposals SET state='SUBMITTING', approval_id=NULL WHERE proposal_id=?", (pid,))  # lost the ack
    g.tick(at(11, 15))
    assert state(db, pid) == "SUBMITTED" and sim.posts == 1


def test_stale_at_apply_is_marked_and_not_retried(env):
    g, sim, db = env
    pid = prop(g, at(11), cfg(voice={"V1": .5, "V2": .5}))
    g.tick(at(11))
    next(iter(sim.approvals.values()))["status"] = "approved"
    sim.apply_status = 409
    g.tick(at(11, 15))
    assert state(db, pid) == "STALE"


def test_queued_proposal_superseded_when_base_moves(env):
    g, sim, db = env
    pid = prop(g, at(22), cfg(voice={"V1": .5, "V2": .5}))
    sim.version = 2  # supervisor edit
    g.tick(at(22, 15))
    assert state(db, pid) == "SUPERSEDED"


def test_batches_disjoint_changes_into_one_request(env):
    g, sim, db = env
    a = prop(g, at(11), cfg(voice={"V1": .5, "V2": .5}), kind="voice")
    b = prop(g, at(11), cfg(welcome={"W1": .5, "W2": .5}), kind="welcome")
    g.tick(at(11))
    assert sim.posts == 1 and state(db, a) == "BATCHED" and state(db, b) == "BATCHED"
    next(iter(sim.approvals.values()))["status"] = "approved"
    g.tick(at(11, 15))
    assert state(db, a) == state(db, b) == "APPLIED"
    assert sim.config["allocations"]["default"]["voice"] == {"V1": .5, "V2": .5}
    assert sim.config["allocations"]["default"]["welcome_message"] == {"W1": .5, "W2": .5}


def test_reserved_slot_only_for_priority(env):
    g, sim, db = env
    for i in range(2):  # spend 2 of 3 slots
        db.execute("INSERT INTO proposals(proposal_id,workflow,kind,state,submitted_ts,via) VALUES(?,?,?,?,?,?)",
                   (f"old{i}", "w1", "k", "APPLIED", at(10).isoformat(), "approval"))
    normal = prop(g, at(12), cfg(voice={"V1": .5, "V2": .5}))
    g.tick(at(12))
    assert state(db, normal) == "QUEUED"
    urgent = prop(g, at(12), cfg(voice={"V1": 0, "V2": 1}), kind="rollback", priority=1)
    g.tick(at(12, 15))
    assert state(db, urgent) == "SUBMITTED"


def test_configdiff_shift_and_dims():
    new = cfg(voice={"V1": .5, "V2": .5})
    assert cd.changed_dims(BASE, new) == {"voice"}
    assert cd.max_shift(BASE, new) == pytest.approx(0.5)
    assert cd.changed_dims(BASE, BASE) == set()
