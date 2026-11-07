"""Planners turn observations into proposals. They are stateless: each tick recomputes from the store, and
the governance layer de-duplicates, so a crash can never leave a planner half-done."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from ..api import Sim
from ..governance import Governance, Policy
from . import callerid, calltime

log = logging.getLogger("controller.plan")


def run(db: sqlite3.Connection, sim: Sim, gov: Governance, wfs: dict[str, dict], pols: dict[str, Policy], now: datetime) -> None:
    for wid, wf in wfs.items():
        cur = sim.get(f"/workflows/{wid}/config")
        for planner in (calltime, callerid):
            planner.plan(db, gov, wf, pols[wid], cur, now)
