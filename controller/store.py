"""SQLite (WAL) store: the controller's only memory and its audit trail."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
-- Aggregated calls: the experiment/detection grain. Only calls actually dialled count as evidence.
CREATE TABLE IF NOT EXISTS call_agg(
  workflow TEXT, day INT, hour INT, version INT, cohort TEXT, lang TEXT, age_band TEXT,
  welcome TEXT, voice TEXT, caller_id TEXT, prompt TEXT,
  dials INT, connected INT, engaged INT, halluc INT,
  PRIMARY KEY(workflow, day, hour, version, cohort, lang, age_band, welcome, voice, caller_id, prompt));
-- Pre-D0 history. Kept apart so it can never be mistaken for experiment evidence.
CREATE TABLE IF NOT EXISTS hist_agg(
  workflow TEXT, hour INT, lang TEXT, age_band TEXT, caller_id TEXT,
  dials INT, connected INT, engaged INT,
  PRIMARY KEY(workflow, hour, lang, age_band, caller_id));
-- One compact row per call: answers "what did this customer hear, and why was it live".
CREATE TABLE IF NOT EXISTS call_audit(
  event_id INTEGER PRIMARY KEY, workflow TEXT, lead_id TEXT, person_id TEXT, ts TEXT,
  version INT, cohort TEXT, welcome TEXT, voice TEXT, caller_id TEXT, prompt TEXT,
  connected INT, engaged INT);
CREATE INDEX IF NOT EXISTS ix_audit_lead ON call_audit(lead_id);
CREATE INDEX IF NOT EXISTS ix_audit_person ON call_audit(person_id);
CREATE TABLE IF NOT EXISTS leads(
  lead_id TEXT PRIMARY KEY, workflow TEXT, person_id TEXT, lang TEXT, city_tier TEXT, age_band TEXT, value_band TEXT);
-- Low-volume world events (config changes, approval decisions, campaigns).
CREATE TABLE IF NOT EXISTS sim_events(event_id INTEGER PRIMARY KEY, type TEXT, workflow TEXT, ts TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS loop_metrics(ts TEXT, sim_ts TEXT, events INT, ingest_s REAL, tick_s REAL, lag_events INT);
"""


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None)  # explicit transactions
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")  # durable across process kill; fine for crash-of-process model
    db.executescript(SCHEMA)
    return db


def meta_get(db: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
