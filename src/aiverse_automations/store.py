from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from .db import connect
from .errors import StateConflict, ValidationError
from .schedule import first_run_at, validate_trigger_spec
from .util import bounded_json, iso, sha256_json, utc_now

ID_LIMIT = 256
WAKE_LIMIT = 65536
WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ACTION_CLASSES = {
    "read_local", "write_local_reversible", "modify_canonical_state", "read_connected",
    "external_write_reversible", "send_message", "publish_publicly", "spend_money",
    "create_commit_or_pr", "merge_or_deploy", "delete_data", "change_permissions",
    "security_sensitive", "high_stakes_domain_action",
}


def _id(value: str, label: str) -> str:
    value = value.strip()
    if not value or len(value) > ID_LIMIT or "\x00" in value or "\n" in value or "\r" in value:
        raise ValidationError(f"invalid {label}")
    return value


def _retry_policy(value: dict[str, Any] | None) -> dict[str, Any]:
    value = value or {}
    if not isinstance(value,dict):
        raise ValidationError("retry policy must be an object")
    allowed = {"max_attempts", "initial_seconds", "backoff", "max_seconds"}
    if set(value) - allowed:
        raise ValidationError("unknown retry policy field")
    result = {
        "max_attempts": int(value.get("max_attempts", 4)),
        "initial_seconds": int(value.get("initial_seconds", 5)),
        "backoff": float(value.get("backoff", 2.0)),
        "max_seconds": int(value.get("max_seconds", 300)),
    }
    if not 1 <= result["max_attempts"] <= 20:
        raise ValidationError("max_attempts must be 1..20")
    if not 1 <= result["initial_seconds"] <= 3600:
        raise ValidationError("initial_seconds must be 1..3600")
    if not 1.0 <= result["backoff"] <= 10.0:
        raise ValidationError("backoff must be 1..10")
    if not result["initial_seconds"] <= result["max_seconds"] <= 86400:
        raise ValidationError("max_seconds is invalid")
    return result


class Store:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir

    def create_automation(self, *, name: str, scope: str, target_kind: str, target_ref: str | None, action_class: str, wake: dict[str, Any], retry: dict[str, Any] | None = None, automation_id: str | None = None) -> dict[str, Any]:
        automation_id = _id(automation_id or f"aut_{uuid4().hex}", "automation id")
        if target_kind not in {"brain", "bot", "team_run", "gateway"}:
            raise ValidationError("target_kind must be brain, bot, team_run or gateway")
        if scope != "operator":
            if not scope.startswith("workspace:") or not WORKSPACE_RE.fullmatch(scope.split(":",1)[1]):
                raise ValidationError("scope must be operator or workspace:<valid-id>")
        if target_kind in {"bot","team_run"} and scope=="operator":
            raise ValidationError("Bot and Team Run automation targets require workspace scope")
        if target_kind in {"bot","team_run"}:
            if not isinstance(target_ref,str) or not target_ref.strip() or len(target_ref)>256:
                raise ValidationError("Bot and Team Run automation targets require bounded target_ref")
        elif target_ref is not None and (not isinstance(target_ref,str) or not target_ref.strip() or len(target_ref)>256):
            raise ValidationError("target_ref must be a bounded non-empty string")
        if action_class not in ACTION_CLASSES:
            raise ValidationError("unknown OS action_class")
        if not isinstance(name,str) or not name.strip() or len(name.strip())>512:
            raise ValidationError("name must be a non-empty string up to 512 characters")
        if not isinstance(wake,dict):
            raise ValidationError("wake must be a JSON object")
        wake_json = bounded_json(wake, max_bytes=WAKE_LIMIT, label="wake")
        retry_json = bounded_json(_retry_policy(retry), max_bytes=4096, label="retry")
        now = utc_now()
        conn = connect(self.state_dir)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO automations(id,name,scope,target_kind,target_ref,action_class,wake_json,retry_json,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (automation_id, name.strip() or automation_id, scope, target_kind, target_ref, action_class, wake_json, retry_json, "active", now, now),
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            raise StateConflict(f"automation id already exists: {automation_id}") from exc
        finally:
            conn.close()
        return self.get_automation(automation_id)

    def get_automation(self, automation_id: str) -> dict[str, Any]:
        conn = connect(self.state_dir)
        try:
            row = conn.execute("SELECT * FROM automations WHERE id=?", (automation_id,)).fetchone()
            if not row:
                raise ValidationError(f"unknown automation: {automation_id}")
            result = dict(row)
            result["wake"] = json.loads(result.pop("wake_json"))
            result["retry"] = json.loads(result.pop("retry_json"))
            return result
        finally:
            conn.close()

    def list_automations(self) -> list[dict[str, Any]]:
        conn = connect(self.state_dir)
        try:
            rows = conn.execute("SELECT * FROM automations ORDER BY created_at,id").fetchall()
            out=[]
            for row in rows:
                x=dict(row); x["wake"]=json.loads(x.pop("wake_json")); x["retry"]=json.loads(x.pop("retry_json")); out.append(x)
            return out
        finally:
            conn.close()

    def set_automation_state(self, automation_id: str, state: str) -> dict[str, Any]:
        if state not in {"active", "paused", "archived"}:
            raise ValidationError("invalid automation state")
        conn=connect(self.state_dir); now=utc_now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur=conn.execute("UPDATE automations SET state=?,version=version+1,updated_at=? WHERE id=?",(state,now,automation_id))
            if cur.rowcount != 1:
                conn.execute("ROLLBACK"); raise ValidationError(f"unknown automation: {automation_id}")
            conn.execute("COMMIT")
        finally: conn.close()
        return self.get_automation(automation_id)

    def update_automation_wake(self, automation_id: str, wake: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(wake,dict): raise ValidationError("wake must be a JSON object")
        wake_json=bounded_json(wake,max_bytes=WAKE_LIMIT,label="wake"); now=utc_now(); conn=connect(self.state_dir)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur=conn.execute("UPDATE automations SET wake_json=?,version=version+1,updated_at=? WHERE id=?",(wake_json,now,automation_id))
            if cur.rowcount != 1:
                conn.execute("ROLLBACK"); raise ValidationError(f"unknown automation: {automation_id}")
            conn.execute("COMMIT")
        finally: conn.close()
        return self.get_automation(automation_id)

    def create_trigger(self, *, automation_id: str, kind: str, spec: dict[str, Any], trigger_id: str | None = None) -> dict[str, Any]:
        self.get_automation(automation_id)
        if not isinstance(spec,dict): raise ValidationError("trigger spec must be an object")
        validate_trigger_spec(kind,spec)
        trigger_id=_id(trigger_id or f"trg_{uuid4().hex}","trigger id")
        spec_json=bounded_json(spec,max_bytes=16384,label="trigger spec")
        next_run=first_run_at(kind,spec)
        now=utc_now(); conn=connect(self.state_dir)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO triggers(id,automation_id,kind,spec_json,state,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(trigger_id,automation_id,kind,spec_json,"active",next_run,now,now))
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK"); raise StateConflict(f"trigger id already exists: {trigger_id}") from exc
        finally: conn.close()
        return self.get_trigger(trigger_id)

    def update_trigger(self, trigger_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        conn=connect(self.state_dir); now=utc_now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row=conn.execute("SELECT kind,state FROM triggers WHERE id=?",(trigger_id,)).fetchone()
            if not row:
                conn.execute("ROLLBACK"); raise ValidationError(f"unknown trigger: {trigger_id}")
            if not isinstance(spec,dict):
                conn.execute("ROLLBACK"); raise ValidationError("trigger spec must be an object")
            validate_trigger_spec(row["kind"],spec)
            spec_json=bounded_json(spec,max_bytes=16384,label="trigger spec")
            next_state="active" if row["state"]=="completed" else row["state"]
            next_run=first_run_at(row["kind"],spec) if next_state=="active" else None
            conn.execute("UPDATE triggers SET spec_json=?,next_run_at=?,state=?,version=version+1,updated_at=? WHERE id=?",(spec_json,next_run,next_state,now,trigger_id))
            conn.execute("COMMIT")
        except Exception:
            try: conn.execute("ROLLBACK")
            except Exception: pass
            raise
        finally: conn.close()
        return self.get_trigger(trigger_id)

    def get_trigger(self, trigger_id: str) -> dict[str, Any]:
        conn=connect(self.state_dir)
        try:
            row=conn.execute("SELECT * FROM triggers WHERE id=?",(trigger_id,)).fetchone()
            if not row: raise ValidationError(f"unknown trigger: {trigger_id}")
            x=dict(row); x["spec"]=json.loads(x.pop("spec_json")); return x
        finally: conn.close()

    def set_trigger_state(self, trigger_id: str, state: str) -> dict[str, Any]:
        if state not in {"active","paused","archived"}: raise ValidationError("invalid trigger state")
        now=utc_now(); conn=connect(self.state_dir)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row=conn.execute("SELECT kind,spec_json FROM triggers WHERE id=?",(trigger_id,)).fetchone()
            if not row:
                conn.execute("ROLLBACK"); raise ValidationError(f"unknown trigger: {trigger_id}")
            next_run=None
            if state=="active": next_run=first_run_at(row["kind"],json.loads(row["spec_json"]))
            conn.execute("UPDATE triggers SET state=?,next_run_at=?,version=version+1,updated_at=? WHERE id=?",(state,next_run,now,trigger_id))
            conn.execute("COMMIT")
        finally: conn.close()
        return self.get_trigger(trigger_id)

    def runs(self, automation_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        conn=connect(self.state_dir)
        try:
            if automation_id:
                rows=conn.execute("SELECT * FROM runs WHERE automation_id=? ORDER BY created_at DESC LIMIT ?",(automation_id,limit)).fetchall()
            else:
                rows=conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()
            out=[]
            for row in rows:
                x=dict(row); x["wake"]=json.loads(x.pop("wake_json")); x["receipt"]=json.loads(x.pop("receipt_json")) if x.get("receipt_json") else None; out.append(x)
            return out
        finally: conn.close()
