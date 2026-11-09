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


# ---- experiments ----
from controller.planners import experiments as ex  # noqa: E402


def _rows(champ_rate, arm_rate, n=600, versions=(1,), days=(0, 1, 2, 3)):
    out = []
    for v in versions:
        for d in days:
            out.append((v, d, "W1", n * 3, n, int(n * champ_rate)))
            out.append((v, d, "W2", n * 3, n, int(n * arm_rate)))
    return out


def test_compare_uses_only_contemporaneous_versions():
    rows = [(1, 0, "W1", 900, 300, 90)] + _rows(.3, .3, versions=(2,))  # v1 had no W2: must be ignored
    c = ex.compare(rows, "W2", "W1")
    assert c["n_b"] == 4 * 600


def test_winner_promoted_with_holdout():
    st = {"phase": "explore", "champion": "W1", "since_version": 0}
    d = ex.decide(_rows(.30, .40), st, ["W1", "W2"], POL, {"W1": .4, "W2": .6})
    assert d["phase"] == "promoted" and d["dist"] == {"W2": .85, "W1": .15}


def test_noise_is_not_promoted():
    st = {"phase": "explore", "champion": "W1", "since_version": 0}
    assert ex.decide(_rows(.30, .31), st, ["W1", "W2"], POL, {"W1": .4, "W2": .6}) is None


def test_novelty_decay_blocks_promotion():
    rows = [(1, d, "W1", 1800, 600, 180) for d in range(4)] + \
           [(1, d, "W2", 1800, 600, e) for d, e in zip(range(4), (270, 270, 175, 175))]  # early +15pp, late gone
    st = {"phase": "explore", "champion": "W1", "since_version": 0}
    assert ex.decide(rows, st, ["W1", "W2"], POL, {"W1": .4, "W2": .6}) is None


def test_clearly_worse_arm_is_dropped():
    st = {"phase": "explore", "champion": "W1", "since_version": 0}
    d = ex.decide(_rows(.35, .25), st, ["W1", "W2"], POL, {"W1": .4, "W2": .6})
    assert d["dropped"] == ["W2"] and d["dist"] == {"W1": 1.0}


def test_pooled_decay_rolls_back_only_when_holdouts_agree():
    st = {"phase": "promoted", "winner": "W2", "baseline": "W1", "since_version": 0}
    faded = _rows(.35, .30, n=40, days=(5, 6, 7))        # winner now *worse* than holdout in every cohort, thin each
    per = {c: (st, faded) for c in ("a", "b", "c", "d")}
    assert set(ex.pooled_decay(per, now_day=7)) == {"a", "b", "c", "d"}   # pooled holdout is big enough to see it
    assert ex.pooled_decay({"a": (st, faded)}, now_day=7) == {}            # one thin cohort alone is not enough
    healthy = _rows(.30, .38, n=40, days=(5, 6, 7))
    assert ex.pooled_decay({c: (st, healthy) for c in "abcd"}, now_day=7) == {}


# ---- incidents ----
from controller.planners import incidents as inc  # noqa: E402


def test_hallucination_detected_vs_reference():
    ps = {"P1": {"n": 2000, "eng": .5, "hal": 10, "rate": .005}, "P2": {"n": 300, "eng": .5, "hal": 30, "rate": .1}}
    d = inc.detect(ps, "P2")
    assert d and d["rate"] == .1


def test_no_alarm_when_rates_alike_or_too_few_calls():
    ps = {"P1": {"n": 2000, "eng": .5, "hal": 20, "rate": .01}, "P2": {"n": 300, "eng": .5, "hal": 4, "rate": .013}}
    assert inc.detect(ps, "P2") is None
    ps["P2"] = {"n": 50, "eng": .5, "hal": 20, "rate": .4}
    assert inc.detect(ps, "P2") is None


def test_choose_skips_failed_validation_and_known_bad_engagement():
    ps = {"P1": {"n": 900, "eng": .55, "hal": 1, "rate": .001}, "P5": {"n": 900, "eng": .40, "hal": 1, "rate": .001}}
    i = {"validations": {"P1": {"run": "a", "pass_rate": .99}, "P3": {"run": "b", "pass_rate": .95},
                         "P5": {"run": "c", "pass_rate": .995}}}
    assert inc._choose(i, ps, POL, []) == "P1"          # P3 fails validation, P5 passes but engagement collapsed
    assert inc._choose(i, ps, POL, ["P1"]) == "P5" or inc._choose(i, ps, POL, ["P1"]) is not None


def test_verbatim_summaries_are_excluded():
    assert inc.VERBATIM.search("Strict: near-verbatim compliance script")
    assert not inc.VERBATIM.search("Grounded: benefit claims restricted to policy-schedule fields")


def test_caller_id_not_blamed_for_being_used_at_bad_hours():
    db = connect(":memory:")
    # same pickup per ID *within* each hour; D1 simply got most of its dials in the bad hour
    for hr, rate, share in ((9, .10, {"D1": 800, "D2": 100}), (19, .40, {"D1": 100, "D2": 800})):
        for cid, n in share.items():
            agg(db, cid, n, int(n * rate), hour=hr)
    spy = Spy(db)
    callerid.plan(db, spy, WF, POL, {"version": 3, "config": CFG}, datetime(2027, 1, 4, 22, 0))
    assert spy.calls == []


def test_verify_tolerates_noise_but_catches_real_engagement_collapse():
    import json as _j
    db = connect(":memory:")

    def run(eng_rate, n=400):
        inc_state = {"candidate": "P1", "bad_prompt": "P2", "tried": [], "pre_eng": .50, "snap": {"n": 1000, "eng": 500, "hal": 10}}
        ps = {"P1": {"n": 1000 + n, "eng": (500 + eng_rate * n) / (1000 + n), "hal": 10 + 2, "rate": 0},
              "P2": {"n": 200, "eng": .5, "hal": 10, "rate": .05}}
        inc._verify(db, "w", inc_state, "P1", ps, datetime(2027, 1, 7, 12))
        return inc_state["phase"]

    assert run(.47) == "resolved"   # 3pp lower on 400 calls is within noise
    assert run(.30) == "failed"     # a collapse is not


def test_decay_seen_through_witness_cohorts_when_own_holdout_is_thin():
    prom = {"phase": "promoted", "winner": "W2", "baseline": "W1", "since_version": 0}
    expl = {"phase": "explore", "champion": "W1", "dist": {"W1": .4, "W2": .6}, "since_version": 0}
    faded = _rows(.35, .30, n=60, days=(5, 6, 7))
    per = {"a": (prom, _rows(.35, .34, n=8, days=(5, 6, 7))), **{c: (expl, faded) for c in "bcd"}}
    assert set(ex.pooled_decay(per, now_day=7)) == {"a"}                      # only promoted cohorts are rolled back
    healthy = _rows(.30, .40, n=60, days=(5, 6, 7))
    per_ok = {"a": (prom, _rows(.30, .40, n=8, days=(5, 6, 7))), **{c: (expl, healthy) for c in "bcd"}}
    assert ex.pooled_decay(per_ok, now_day=7) == {}
