from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from aiverse_automations.config import load_config, save_config
from aiverse_automations.engine import Engine
from aiverse_automations.store import Store


def configure_gateway(state_dir: Path, url: str):
    cfg=load_config(state_dir,required=True); cfg["targets"]["gateway"]={"url":url}; save_config(state_dir,cfg)


def test_due_schedule_dispatches_once_with_stable_invocation(state_dir, allow_os, owner_server):
    url,received,_=owner_server; configure_gateway(state_dir,url)
    store=Store(state_dir)
    a=store.create_automation(name="daily",scope="operator",target_kind="gateway",target_ref="main",action_class="read_local",wake={"prompt":"hello"})
    t=store.create_trigger(automation_id=a["id"],kind="once",spec={"at":"2026-01-01T00:00:00Z"})
    engine=Engine(state_dir)
    runs=engine.tick(now=datetime(2026,1,1,0,0,1,tzinfo=timezone.utc))
    assert len(runs)==1 and runs[0]["status"]=="succeeded"
    assert len(received)==1
    first_id=received[0]["invocation_id"]
    assert first_id.startswith("inv_")
    assert engine.tick(now=datetime(2026,1,1,0,1,tzinfo=timezone.utc))==[]
    assert len(received)==1
    assert store.get_trigger(t["id"])["state"]=="completed"


def test_pause_or_edit_is_execution_fence(state_dir, allow_os, owner_server):
    url,received,_=owner_server; configure_gateway(state_dir,url)
    store=Store(state_dir)
    a=store.create_automation(name="fence",scope="operator",target_kind="gateway",target_ref=None,action_class="read_local",wake={"prompt":"old"})
    t=store.create_trigger(automation_id=a["id"],kind="once",spec={"at":"2026-01-01T00:00:00Z"})
    engine=Engine(state_dir)
    claimed=engine._claim_due(datetime(2026,1,1,0,0,1,tzinfo=timezone.utc))
    assert len(claimed)==1
    store.update_automation_wake(a["id"],{"prompt":"new"})
    result=engine.execute_run(claimed[0])
    assert result["status"]=="canceled"
    assert result["last_error_code"]=="DEFINITION_REVOKED"
    assert received==[]


def test_retry_rechecks_authority_and_revocation_blocks(state_dir, monkeypatch, owner_server):
    url,received,behavior=owner_server; configure_gateway(state_dir,url); behavior["status"]=503
    decisions=["allow","deny"]
    def auth(*,os_root,request,timeout_seconds=10.0):
        return {**request,"decision":decisions.pop(0),"reason":"test"}
    monkeypatch.setattr("aiverse_automations.engine.os_permission",auth)
    store=Store(state_dir)
    a=store.create_automation(name="retry",scope="operator",target_kind="gateway",target_ref=None,action_class="read_local",wake={},retry={"max_attempts":3,"initial_seconds":1,"backoff":1,"max_seconds":1})
    store.create_trigger(automation_id=a["id"],kind="once",spec={"at":"2026-01-01T00:00:00Z"})
    engine=Engine(state_dir)
    first=engine.tick(now=datetime(2026,1,1,0,0,1,tzinfo=timezone.utc))[0]
    assert first["status"]=="retry_wait" and len(received)==1
    # Force retry due without sleeping.
    from aiverse_automations.db import connect
    conn=connect(state_dir); conn.execute("UPDATE runs SET next_attempt_at='2026-01-01T00:00:02Z' WHERE id=?",(first["id"],)); conn.close()
    behavior["status"]=200
    second=engine.tick(now=datetime(2026,1,1,0,0,3,tzinfo=timezone.utc))[0]
    assert second["status"]=="blocked"
    assert second["last_error_code"]=="OS_DENIED"
    assert len(received)==1
