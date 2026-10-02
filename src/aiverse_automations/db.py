from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import sqlite3

from .errors import ValidationError

SCHEMA_VERSION = 2
OWNER_ID = "ai-verse-automations"
APPLICATION_ID = 0x41564155

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

# Schema version 1 is the only supported pre-v2 migration source. Its sole
# structural difference is the absence of runs.claim_owner.
LEGACY_SCHEMA_V1 = SCHEMA.replace("  claim_owner TEXT,\n", "", 1)


def db_path(state_dir: Path) -> Path:
    return state_dir / "automations.db"


def _raw_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _pragma_arg(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _normalize_declared_type(value: object) -> str:
    return " ".join(str(value or "").upper().split())


def _schema_signature(conn: sqlite3.Connection) -> tuple:
    tables = tuple(
        sorted(
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        )
    )
    details = []
    for table in tables:
        table_arg = _pragma_arg(table)
        columns = tuple(
            sorted(
                (
                    str(row["name"]),
                    _normalize_declared_type(row["type"]),
                    int(row["notnull"]),
                    None if row["dflt_value"] is None else str(row["dflt_value"]),
                    int(row["pk"]),
                )
                for row in conn.execute(f"PRAGMA table_info({table_arg})").fetchall()
            )
        )
        foreign_keys = tuple(
            sorted(
                (
                    str(row["from"]),
                    None if row["to"] is None else str(row["to"]),
                    str(row["table"]),
                    str(row["on_update"]),
                    str(row["on_delete"]),
                    str(row["match"]),
                )
                for row in conn.execute(f"PRAGMA foreign_key_list({table_arg})").fetchall()
            )
        )
        indexes = []
        for row in conn.execute(f"PRAGMA index_list({table_arg})").fetchall():
            index_name = str(row["name"])
            index_arg = _pragma_arg(index_name)
            columns_for_index = tuple(
                None if item["name"] is None else str(item["name"])
                for item in conn.execute(f"PRAGMA index_info({index_arg})").fetchall()
            )
            origin = str(row["origin"])
            stable_name = index_name if origin == "c" else f"<sqlite-{origin}>"
            indexes.append(
                (
                    stable_name,
                    int(row["unique"]),
                    origin,
                    int(row["partial"]),
                    columns_for_index,
                )
            )
        details.append(
            (
                table,
                columns,
                foreign_keys,
                tuple(sorted(indexes)),
            )
        )
    return tables, tuple(details)


@lru_cache(maxsize=2)
def _expected_schema_signature(schema: str) -> tuple:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(schema)
        return _schema_signature(conn)
    finally:
        conn.close()


def _meta_values(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = conn.execute("SELECT key,value FROM meta").fetchall()
    except sqlite3.DatabaseError as exc:
        raise ValidationError("Automations store metadata is unreadable") from exc
    return {str(row["key"]): str(row["value"]) for row in rows}


def _identity_values(conn: sqlite3.Connection) -> tuple[int, int]:
    application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    return application_id, user_version


def _classify_store(
    conn: sqlite3.Connection,
    *,
    allow_legacy: bool,
) -> str:
    signature = _schema_signature(conn)
    current_signature = _expected_schema_signature(SCHEMA)
    legacy_v1_signature = _expected_schema_signature(LEGACY_SCHEMA_V1)

    if signature not in {current_signature, legacy_v1_signature}:
        raise ValidationError(
            "Automations store schema is foreign or incompatible; refusing canonical database mutation"
        )

    meta = _meta_values(conn)
    application_id, user_version = _identity_values(conn)
    owner_id = meta.get("owner_id")
    meta_version = meta.get("schema_version")

    if signature == current_signature:
        if (
            application_id == APPLICATION_ID
            and user_version == SCHEMA_VERSION
            and owner_id == OWNER_ID
            and meta_version == str(SCHEMA_VERSION)
        ):
            return "current"

        if (
            allow_legacy
            and application_id == 0
            and user_version == 0
            and owner_id is None
            and meta_version == str(SCHEMA_VERSION)
        ):
            return "legacy-v2-unmarked"

        raise ValidationError(
            "Automations store ownership or format identity does not match this component"
        )

    if (
        allow_legacy
        and application_id == 0
        and user_version == 0
        and owner_id is None
        and meta_version == "1"
    ):
        return "legacy-v1"

    raise ValidationError(
        "Automations store is not a supported prior format and will not be migrated"
    )


def _stamp_current_identity(
    conn: sqlite3.Connection,
    *,
    legacy_kind: str | None = None,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        if legacy_kind == "legacy-v1":
            conn.execute("ALTER TABLE runs ADD COLUMN claim_owner TEXT")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('owner_id',?)",
            (OWNER_ID,),
        )
        conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def _validate_current_store(conn: sqlite3.Connection) -> None:
    _classify_store(conn, allow_legacy=False)


def connect(state_dir: Path) -> sqlite3.Connection:
    path = db_path(state_dir)
    if not path.is_file() or path.stat().st_size == 0:
        raise ValidationError(
            "Automations canonical store is not initialized; run component setup first"
        )

    conn = _raw_connect(path)
    try:
        _validate_current_store(conn)
    except Exception:
        conn.close()
        raise

    if os.name != "nt":
        os.chmod(path, 0o600)
    return conn


def initialize(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(state_dir, 0o700)

    path = db_path(state_dir)
    fresh = not path.exists() or path.stat().st_size == 0
    conn = _raw_connect(path)
    try:
        if fresh:
            conn.executescript(SCHEMA)
            _stamp_current_identity(conn)
        else:
            classification = _classify_store(conn, allow_legacy=True)
            if classification != "current":
                _stamp_current_identity(conn, legacy_kind=classification)

        _validate_current_store(conn)
    finally:
        conn.close()

    if os.name != "nt":
        os.chmod(path, 0o600)


def integrity_check(state_dir: Path) -> tuple[bool, str]:
    path = db_path(state_dir)
    if not path.is_file() or path.stat().st_size == 0:
        return False, "database missing or empty"

    conn = _raw_connect(path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        value = str(row[0]) if row else "missing result"
        if value != "ok":
            return False, value
        try:
            _validate_current_store(conn)
        except ValidationError as exc:
            return False, str(exc)
        return True, "ok"
    except sqlite3.DatabaseError as exc:
        return False, f"SQLite error: {exc}"
    finally:
        conn.close()
