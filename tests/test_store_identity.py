from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aiverse_automations.db import (
    APPLICATION_ID,
    LEGACY_SCHEMA_V1,
    OWNER_ID,
    SCHEMA,
    SCHEMA_VERSION,
    db_path,
    initialize,
    integrity_check,
)
from aiverse_automations.errors import ValidationError
from aiverse_automations.lifecycle import descriptor, doctor, setup, update


def _os_root(base: Path) -> Path:
    root = base / "os"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "action-permission.mjs").write_text("// test boundary\n", encoding="utf-8")
    return root


def _raw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _markers(state_dir: Path) -> tuple[int, int, dict[str, str]]:
    conn = _raw(db_path(state_dir))
    try:
        application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        meta = {
            str(row["key"]): str(row["value"])
            for row in conn.execute("SELECT key,value FROM meta").fetchall()
        }
        return application_id, user_version, meta
    finally:
        conn.close()


def test_clean_setup_stamps_durable_owner_and_format_identity(tmp_path: Path):
    state = tmp_path / "state"
    report = setup(state, os_root=str(_os_root(tmp_path)), enable=True)

    assert report["state"] == "ready"
    application_id, user_version, meta = _markers(state)
    assert application_id == APPLICATION_ID
    assert user_version == SCHEMA_VERSION
    assert meta["owner_id"] == OWNER_ID
    assert meta["schema_version"] == str(SCHEMA_VERSION)
    assert integrity_check(state) == (True, "ok")


def test_foreign_valid_sqlite_is_refused_before_any_schema_write(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    path = db_path(state)
    conn = _raw(path)
    try:
        conn.execute("CREATE TABLE foreign_records(id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO foreign_records(id,value) VALUES('foreign','keep-me')")
        conn.commit()
    finally:
        conn.close()

    before = path.read_bytes()
    with pytest.raises(ValidationError, match="foreign or incompatible"):
        setup(state, os_root=str(_os_root(tmp_path)), enable=True)

    assert path.read_bytes() == before
    conn = _raw(path)
    try:
        assert conn.execute("SELECT value FROM foreign_records WHERE id='foreign'").fetchone()[0] == "keep-me"
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name='automations'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name='meta'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_known_unmarked_v2_store_is_adopted_only_after_exact_schema_match(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    conn = _raw(db_path(state))
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    finally:
        conn.close()

    initialize(state)
    application_id, user_version, meta = _markers(state)
    assert application_id == APPLICATION_ID
    assert user_version == SCHEMA_VERSION
    assert meta["owner_id"] == OWNER_ID
    assert integrity_check(state) == (True, "ok")


def test_known_v1_store_migrates_claim_owner_then_stamps_current_identity(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    conn = _raw(db_path(state))
    try:
        conn.executescript(LEGACY_SCHEMA_V1)
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','1')")
        conn.commit()
    finally:
        conn.close()

    initialize(state)
    conn = _raw(db_path(state))
    try:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    finally:
        conn.close()
    assert "claim_owner" in columns
    application_id, user_version, meta = _markers(state)
    assert application_id == APPLICATION_ID
    assert user_version == SCHEMA_VERSION
    assert meta["owner_id"] == OWNER_ID
    assert meta["schema_version"] == str(SCHEMA_VERSION)


def test_update_adopts_only_known_compatible_unmarked_store(state_dir: Path):
    path = db_path(state_dir)
    conn = _raw(path)
    try:
        conn.execute("DELETE FROM meta WHERE key='owner_id'")
        conn.execute("PRAGMA application_id=0")
        conn.execute("PRAGMA user_version=0")
        conn.commit()
    finally:
        conn.close()

    assert integrity_check(state_dir)[0] is False
    report = update(state_dir)
    assert report["state"] == "ready"
    assert _markers(state_dir)[:2] == (APPLICATION_ID, SCHEMA_VERSION)


@pytest.mark.parametrize(
    "damage_sql",
    [
        "DROP TABLE wake_receipts",
        "DROP INDEX idx_runs_retry",
    ],
)
def test_weakened_or_missing_required_schema_is_never_repaired_in_place(
    state_dir: Path,
    damage_sql: str,
):
    path = db_path(state_dir)
    conn = _raw(path)
    try:
        conn.execute(damage_sql)
        conn.commit()
    finally:
        conn.close()

    ok, detail = integrity_check(state_dir)
    assert ok is False
    assert "foreign or incompatible" in detail
    with pytest.raises(ValidationError, match="foreign or incompatible"):
        update(state_dir)


def test_wrong_schema_version_makes_status_and_doctor_unhealthy_without_mutation(state_dir: Path):
    path = db_path(state_dir)
    conn = _raw(path)
    try:
        conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
        conn.commit()
    finally:
        conn.close()

    report = descriptor(state_dir)
    assert report["state"] == "unhealthy"
    assert report["health"]["database"] is False

    diagnosis = doctor(state_dir)
    assert diagnosis["ok"] is False
    assert diagnosis["state"] == "unhealthy"
    sqlite_check = next(item for item in diagnosis["checks"] if item["name"] == "sqlite-integrity")
    assert sqlite_check["ok"] is False
    assert "ownership or format identity" in sqlite_check["detail"]
    assert any(item["name"] == "store-operational" for item in diagnosis["checks"])

    with pytest.raises(ValidationError, match="ownership or format identity"):
        update(state_dir)
