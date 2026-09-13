from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiverse_automations.config import load_config, save_config
from aiverse_automations.db import connect
from aiverse_automations.engine import Engine
from aiverse_automations.store import Store


def test_restart_marks_abandoned_nonterminal_unknown(state_dir,allow_os,owner_server):
    url,_,_=owner_server; cfg=load_config(state_dir,required=True); cfg["targets"]["gateway"]={"url":url}; save_config(state_dir,cfg)
    store=Store(state_dir); a=store.create_automation(name="recover",scope="operator",target_kind="gateway",target_ref=None,action_class="read_local",wake={})
    store.create_trigger(automation_id=a["id"],kind="once",spec={"at":"2026-01-01T00:00:00Z"})
    engine=Engine(state_dir); ids=engine._claim_due(datetime(2026,1,1,0,0,1,tzinfo=timezone.utc)); run_id=ids[0]
    conn=connect(state_dir); conn.execute("UPDATE runs SET status='delivering',updated_at='2026-01-01T00:00:01Z' WHERE id=?",(run_id,)); conn.close()
    recovered=Engine(state_dir,owner_id="new-process").recover_stale(now=datetime(2026,1,1,0,1,tzinfo=timezone.utc))
    assert recovered==[run_id]
    assert engine.get_run(run_id)["status"]=="unknown"


def test_live_owner_is_not_reclaimed_by_timeout_alone(state_dir,allow_os,owner_server):
    url,_,_=owner_server; cfg=load_config(state_dir,required=True); cfg["targets"]["gateway"]={"url":url}; save_config(state_dir,cfg)
    store=Store(state_dir); a=store.create_automation(name="live",scope="operator",target_kind="gateway",target_ref=None,action_class="read_local",wake={})
    store.create_trigger(automation_id=a["id"],kind="once",spec={"at":"2026-01-01T00:00:00Z"})
    engine=Engine(state_dir,owner_id="live-process")
    run_id=engine._claim_due(datetime(2026,1,1,0,0,1,tzinfo=timezone.utc))[0]
    conn=connect(state_dir); conn.execute("UPDATE runs SET status='delivering',updated_at='2026-01-01T00:00:01Z' WHERE id=?",(run_id,)); conn.close()
    assert engine.recover_stale(now=datetime(2026,1,1,1,0,tzinfo=timezone.utc))==[]
    assert engine.get_run(run_id)["status"]=="delivering"
