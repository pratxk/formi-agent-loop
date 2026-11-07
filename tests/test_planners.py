"""Planner decisions on synthetic data: no simulator, no governance side effects."""
import copy
from datetime import datetime

import pytest

from controller import stats
from controller.governance import Policy
from controller.planners import callerid, calltime
from controller.store import connect

WF = {"workflow_id": "w", "daily_batch": {"size": 1000}, "options": {"caller_id": ["D1", "D2", "D3"]}}
POL = Policy("w", "t", (10, 19), 120, 24, 3, True, frozenset({"schedule", "caller_id"}), 0.5, 0.97, "allowed",
             "engagement_rate", {"pickup_rate_max_drop": 0.02})
CFG = {"cohorts": [], "allocations": {"default": {"caller_id": {"D1": .5, "D2": .5}, "voice": {"V1": 1}}},
       "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}


class Spy:
    def __init__(self, db):
        self.db, self.calls = db, []

    def propose(self, **kw):
        self.calls.append(kw)


def agg(db, caller, dials, connected, hour=10, day=0):
    db.execute("INSERT INTO call_agg VALUES('w',?,?,1,'default','hi','31-45','W1','V1',?,'P1',?,?,0,0)",
               (day, hour, caller, dials, connected))


def test_stats_sanity():
    assert stats.two_prop_z(100, 1000, 100, 1000) == 0
    assert stats.two_prop_z(50, 1000, 150, 1000) < -6
    assert stats.chi2_sf(5, 5) > 0.3 and stats.chi2_sf(200, 10) < 1e-9


def test_caller_id_collapse_is_flagged_and_rotated():
    db = connect(":memory:")
    agg(db, "D1", 600, 60)   # 10% pickup
    agg(db, "D2", 600, 180)  # 30% pickup
    spy = Spy(db)
    callerid.plan(db, spy, WF, POL, {"version": 3, "config": CFG}, datetime(2027, 1, 4, 12, 0))
    (p,) = spy.calls
    new = p["config"]["allocations"]["default"]["caller_id"]
    assert new["D1"] < 0.5 and new["D2"] > 0.5 and p["priority"] == 1
    assert sum(new.values()) == pytest.approx(1, abs=1e-3)


def test_caller_id_no_false_alarm_on_noise():
    db = connect(":memory:")
    agg(db, "D1", 600, 160)
    agg(db, "D2", 600, 175)
    spy = Spy(db)
    callerid.plan(db, spy, WF, POL, {"version": 3, "config": CFG}, datetime(2027, 1, 4, 12, 0))
    assert spy.calls == []


def test_caller_id_respects_auto_apply_shift_cap_and_probes_a_spare():
    db = connect(":memory:")
    agg(db, "D1", 600, 30)
    agg(db, "D2", 600, 180)
    pol = Policy("w", "t", (10, 19), 120, 24, 3, True, frozenset({"caller_id"}), 0.2, 0.97, "allowed", "x", {})
    spy = Spy(db)
    callerid.plan(db, spy, WF, pol, {"version": 3, "config": CFG}, datetime(2027, 1, 4, 12, 0))
    new = spy.calls[0]["config"]["allocations"]["default"]["caller_id"]
    assert abs(new["D1"] - 0.5) <= 0.2 + 1e-6  # one staged step, never above max_shift


def test_hours_follow_pickup_and_weights_are_floored():
    prof = {h: (1000, 300 if h < 12 else 150, 100) for h in range(9, 21)}
    w = calltime._weights(prof, 1)
    assert w[10] > w[15] and min(w.values()) >= 0.2 and sum(w.values()) / len(w) == pytest.approx(1, abs=0.02)


def test_calltime_plans_cohorts_only_when_segments_really_differ():
    db = connect(":memory:")
    for seg, good in (("old", range(9, 13)), ("young", range(17, 21))):
        for h in range(9, 21):
            rate = 0.35 if h in good else 0.15
            db.execute("INSERT INTO hist_agg VALUES('w',?,?,?, 'D1',?,?,?)", (h, "hi", seg, 800, int(800 * rate), int(800 * rate * .5)))
    spy = Spy(db)
    calltime.plan(db, spy, {**WF, "workflow_id": "w"}, POL, {"version": 1, "config": copy.deepcopy(CFG)}, datetime(2027, 1, 4))
    (p,) = spy.calls
    assert {c["id"] for c in p["config"]["cohorts"]} == {"age_band_old", "age_band_young"}
    hw = p["config"]["schedule"]["hour_weights"]
    assert hw["age_band_old"][10] > hw["age_band_old"][19] and hw["age_band_young"][19] > hw["age_band_young"][10]
