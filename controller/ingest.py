"""Chunk ingestion. One transaction per chunk: aggregates, audit rows and the cursor commit together,
so a kill at any instant leaves either 'chunk fully applied' or 'chunk not applied' (exactly-once)."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date

from .api import Sim
from .store import meta_get, meta_set

D0 = date(2027, 1, 4)
AGG_KEYS = ("workflow", "day", "hour", "version", "cohort", "lang", "age_band", "welcome", "voice", "caller_id", "prompt")


class Ingestor:
    def __init__(self, db: sqlite3.Connection, sim: Sim) -> None:
        self.db, self.sim = db, sim
        # lead_id -> (lang, age_band). Small tuples; the only per-lead state held in memory.
        self.leads: dict[str, tuple[str, str]] = {
            r[0]: (r[1], r[2]) for r in db.execute("SELECT lead_id, lang, age_band FROM leads")
        }

    def pending(self, kind: str, key: str) -> list[dict]:
        after = int(meta_get(self.db, key) or -1)
        return self.sim.get("/events/chunks", params={"kind": kind, "after": after})

    def ingest_all(self) -> int:
        """Ingest history (once) then any event chunks newer than the cursor. Returns events ingested."""
        n = 0
        for ch in self.pending("history", "hist_chunk"):
            n += self._ingest(ch["chunk"], "hist_chunk", history=True)
        for ch in self.pending("events", "last_chunk"):
            n += self._ingest(ch["chunk"], "last_chunk", history=False)
        return n

    def _ingest(self, chunk: int, cursor_key: str, history: bool) -> int:
        lines = self.sim.chunk_lines(chunk)
        agg: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
        audit, new_leads, sim_events = [], [], []
        for line in lines:
            e = json.loads(line)
            t = e["type"]
            if t == "call":
                lang, age = self.leads.get(e["lead_id"], ("?", "?"))
                v = e["variant"]
                if history:
                    k = (e["workflow_id"], int(e["ts"][11:13]), lang, age, v["caller_id"])
                else:
                    day = (date.fromisoformat(e["ts"][:10]) - D0).days
                    k = (e["workflow_id"], day, int(e["ts"][11:13]), e["config_version"], e["cohort_id"], lang, age,
                         v["welcome_message"], v["voice"], v["caller_id"], v["prompt_version"])
                    audit.append((e["event_id"], e["workflow_id"], e["lead_id"], e["person_id"], e["ts"],
                                  e["config_version"], e["cohort_id"], v["welcome_message"], v["voice"],
                                  v["caller_id"], v["prompt_version"], int(e["connected"]), int(e["engaged"])))
                a = agg[k]
                a[0] += 1
                a[1] += e["connected"]
                a[2] += e["engaged"]
                a[3] += bool((e.get("qa") or {}).get("hallucination_flag"))
            elif t == "lead":
                f = e["features"]
                self.leads[e["lead_id"]] = (f["lang"], f["age_band"])
                new_leads.append((e["lead_id"], e["workflow_id"], e["person_id"], f["lang"], f["city_tier"],
                                  f["age_band"], f["value_band"]))
            else:
                sim_events.append((e["event_id"], t, e.get("workflow_id"), e["ts"], line))
        db = self.db
        db.execute("BEGIN")
        try:
            db.executemany("INSERT OR IGNORE INTO leads VALUES(?,?,?,?,?,?,?)", new_leads)
            db.executemany("INSERT OR IGNORE INTO call_audit VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", audit)
            db.executemany("INSERT OR IGNORE INTO sim_events VALUES(?,?,?,?,?)", sim_events)
            if history:
                db.executemany(
                    "INSERT INTO hist_agg VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(workflow,hour,lang,age_band,caller_id) "
                    "DO UPDATE SET dials=dials+excluded.dials, connected=connected+excluded.connected, "
                    "engaged=engaged+excluded.engaged",
                    [(*k, *v[:3]) for k, v in agg.items()])
            else:
                db.executemany(
                    "INSERT INTO call_agg VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(workflow,day,hour,version,"
                    "cohort,lang,age_band,welcome,voice,caller_id,prompt) DO UPDATE SET dials=dials+excluded.dials, "
                    "connected=connected+excluded.connected, engaged=engaged+excluded.engaged, "
                    "halluc=halluc+excluded.halluc",
                    [(*k, *v) for k, v in agg.items()])
            meta_set(db, cursor_key, str(chunk))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return len(lines)
