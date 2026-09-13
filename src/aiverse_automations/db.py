from __future__ import annotations

import sqlite3
import os
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = r'''
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS automations (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  scope TEXT NOT NULL,
  target_kind TEXT NOT NULL,
  target_ref TEXT,
  action_class TEXT NOT NULL,
  wake_json TEXT NOT NULL,
  retry_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','paused','archived')),
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS triggers (
  id TEXT PRIMARY KEY,
  automation_id TEXT NOT NULL REFERENCES automations(id),
  kind TEXT NOT NULL CHECK(kind IN ('once','interval','cron','webhook','event')),
  spec_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','paused','completed','archived')),
  version INTEGER NOT NULL DEFAULT 1,
  next_run_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_triggers_due ON triggers(state, next_run_at);
CREATE INDEX IF NOT EXISTS idx_triggers_automation ON triggers(automation_id);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  invocation_id TEXT NOT NULL UNIQUE,
  automation_id TEXT NOT NULL REFERENCES automations(id),
  automation_version INTEGER NOT NULL,
  trigger_id TEXT NOT NULL REFERENCES triggers(id),
  trigger_version INTEGER NOT NULL,
  source_kind TEXT NOT NULL,
  scheduled_for TEXT,
  fired_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN (
    'claimed','authorizing','delivering','retry_wait','succeeded','failed',
    'dead_letter','blocked','unknown','canceled'
  )),
  attempt INTEGER NOT NULL DEFAULT 0,
  claim_owner TEXT,
  next_attempt_at TEXT,
  request_fingerprint TEXT NOT NULL,
  wake_json TEXT NOT NULL,
  receipt_json TEXT,
  last_error_code TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_retry ON runs(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_runs_automation ON runs(automation_id, created_at);

CREATE TABLE IF NOT EXISTS event_receipts (
  trigger_id TEXT NOT NULL REFERENCES triggers(id),
  event_id TEXT NOT NULL,
  event_digest TEXT NOT NULL,
  received_at TEXT NOT NULL,
  run_id TEXT,
  PRIMARY KEY(trigger_id, event_id)
);

CREATE TABLE IF NOT EXISTS wake_receipts (
  invocation_id TEXT PRIMARY KEY,
  owner_kind TEXT NOT NULL,
  owner_receipt_json TEXT NOT NULL,
  owner_receipt_digest TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
'''


def db_path(state_dir: Path) -> Path:
    return state_dir / "automations.db"


def connect(state_dir: Path) -> sqlite3.Connection:
    state_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(state_dir, 0o700)
    path=db_path(state_dir)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    if os.name != "nt":
        os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def initialize(state_dir: Path) -> None:
    conn = connect(state_dir)
    try:
        conn.executescript(SCHEMA)
        columns={row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "claim_owner" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN claim_owner TEXT")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    finally:
        conn.close()


def integrity_check(state_dir: Path) -> tuple[bool, str]:
    conn = connect(state_dir)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        value = str(row[0]) if row else "missing result"
        return value == "ok", value
    finally:
        conn.close()
