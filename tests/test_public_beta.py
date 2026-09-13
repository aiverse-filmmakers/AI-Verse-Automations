from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading

import pytest

from aiverse_automations.adapters import _safe_http_target
from aiverse_automations.config import load_config, save_config
from aiverse_automations.engine import Engine
from aiverse_automations.errors import StateConflict, ValidationError
from aiverse_automations.lifecycle import descriptor, setup
from aiverse_automations.schedule import first_run_at, parse_cron
from aiverse_automations.store import Store
from aiverse_automations.webhook import WebhookServer


def _target(state_dir: Path, kind: str, config: dict):
    cfg=load_config(state_dir,required=True); cfg["targets"][kind]=config; save_config(state_dir,cfg)


def test_cron_standard_fields_names_and_timezone():
    parse_cron("*/15 9-17 * JAN,MAR MON-FRI")
    nxt=first_run_at("cron",{"expr":"0 9 * * MON","timezone":"Europe/Bucharest"},now=datetime(2026,9,13,12,0,tzinfo=timezone.utc))
    assert nxt=="2026-09-14T06:00:00Z"


def test_event_source_and_type_are_authority_bound(state_dir, allow_os, owner_server):
    url,received,_=owner_server; _target(state_dir,"gateway",{"url":url})
    store=Store(state_dir); a=store.create_automation(name="event",scope="operator",target_kind="gateway",target_ref=None,action_class="read_local",wake={})
    t=store.create_trigger(automation_id=a["id"],kind="event",spec={"source":"calendar","event_type":"changed"})
    engine=Engine(state_dir)
    with pytest.raises(ValidationError):
        engine.ingest_event(trigger_id=t["id"],event_id="evt-wrong-source",event={"x":1},source_kind="event",source="email",event_type="changed")
    with pytest.raises(ValidationError):
        engine.ingest_event(trigger_id=t["id"],event_id="evt-wrong-type",event={"x":1},source_kind="event",source="calendar",event_type="deleted")
    result=engine.ingest_event(trigger_id=t["id"],event_id="evt-ok",event={"x":1},source_kind="event",source="calendar",event_type="changed")
    assert result["status"]=="succeeded" and len(received)==1


def test_restart_unknown_requires_explicit_idempotency_confirmation(state_dir,allow_os,owner_server):
    url,received,_=owner_server; _target(state_dir,"gateway",{"url":url})
    store=Store(state_dir); a=store.create_automation(name="crash",scope="operator",target_kind="gateway",target_ref=None,action_class="read_local",wake={})
    store.create_trigger(automation_id=a["id"],kind="once",spec={"at":"2026-01-01T00:00:00Z"})
    engine=Engine(state_dir); run_id=engine._claim_due(datetime(2026,1,1,0,0,1,tzinfo=timezone.utc))[0]
    from aiverse_automations.db import connect
    conn=connect(state_dir); conn.execute("UPDATE runs SET status='delivering',updated_at='2026-01-01T00:00:01Z' WHERE id=?",(run_id,)); conn.close()
    restarted=Engine(state_dir,owner_id="new-process")
    restarted.recover_stale(now=datetime(2026,1,1,0,0,5,tzinfo=timezone.utc))
    original_invocation=restarted.get_run(run_id)["invocation_id"]
    with pytest.raises(StateConflict): restarted.retry_run(run_id)
    recovered=restarted.retry_run(run_id,owner_idempotency_confirmed=True)
    assert recovered["status"]=="succeeded"
    assert received[0]["invocation_id"]==original_invocation


def test_current_multiple_bots_adapter_emits_phase_36_contract_and_projection(state_dir,allow_os,owner_server):
    url,received,_=owner_server
    cfg=load_config(state_dir,required=True); os_root=Path(cfg["os_root"])
    _target(state_dir,"bot",{"url":url,"protocol":"ai-verse-multiple-bots-v1"})
    store=Store(state_dir)
    a=store.create_automation(name="bot wake",scope="workspace:demo",target_kind="bot",target_ref="bot_editor",action_class="read_local",wake={"objective":"Review the cut","tools":["read-local"],"maxHops":2})
    store.create_trigger(automation_id=a["id"],kind="once",spec={"at":"2099-01-01T00:00:00Z"})
    result=Engine(state_dir).run_now(a["id"])
    assert result["status"]=="succeeded"
    body=received[0]
    assert body["automationId"]==a["id"]
    assert body["workspaceId"]=="demo"
    assert body["target"]=={"kind":"bot","botId":"bot_editor"}
    assert body["objective"]=="Review the cut"
    assert body["tools"]==["read-local"]
    source=body["source"]
    projection=os_root/source["path"]
    assert projection.is_file() and not projection.is_symlink()
    assert hashlib.sha256(projection.read_bytes()).hexdigest()==source["digest"]
    projected=json.loads(projection.read_text(encoding="utf-8"))
    assert projected["projection_only"] is True
    assert "Review the cut" not in projection.read_text(encoding="utf-8")


def test_due_claim_is_concurrency_safe(state_dir,allow_os,owner_server):
    url,received,_=owner_server; _target(state_dir,"gateway",{"url":url})
    store=Store(state_dir); a=store.create_automation(name="race",scope="operator",target_kind="gateway",target_ref=None,action_class="read_local",wake={})
    store.create_trigger(automation_id=a["id"],kind="once",spec={"at":"2026-01-01T00:00:00Z"})
    barrier=threading.Barrier(2); results=[]; errors=[]
    def work():
        try:
            barrier.wait(); results.extend(Engine(state_dir).tick(now=datetime(2026,1,1,0,0,1,tzinfo=timezone.utc)))
        except Exception as exc: errors.append(exc)
    threads=[threading.Thread(target=work) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=5)
    assert errors==[]
    assert len(store.runs(a["id"]))==1
    assert len(received)==1


def test_setup_detects_legacy_definition_authority(tmp_path: Path):
    os_root=tmp_path/"os"; (os_root/"scripts").mkdir(parents=True); (os_root/"scripts"/"action-permission.mjs").write_text("// permission",encoding="utf-8")
    jobs=os_root/"automations"/"jobs"; jobs.mkdir(parents=True); legacy=jobs/"daily.yaml"; legacy.write_text("schedule: daily\n",encoding="utf-8")
    state=tmp_path/"state"
    report=setup(state,os_root=str(os_root),enable=True)
    assert report["state"]=="migration-required" and report["enabled"] is False
    assert report["migration_sources"]==["automations/jobs/daily.yaml"]
    legacy.unlink()
    report=setup(state,os_root=str(os_root),enable=True)
    assert report["state"]=="ready" and report["migration_required"] is False


def test_public_beta_http_server_refuses_remote_bind(state_dir):
    with pytest.raises(ValidationError): WebhookServer(state_dir,"0.0.0.0",0)


def test_plain_http_url_rejects_embedded_credentials():
    with pytest.raises(ValidationError): _safe_http_target("http://user:pass@127.0.0.1:9999/invoke")


def test_automation_pause_resume_preserves_trigger_state(state_dir):
    store=Store(state_dir)
    a=store.create_automation(name="pause-parent",scope="operator",target_kind="gateway",target_ref=None,action_class="read_local",wake={})
    t=store.create_trigger(automation_id=a["id"],kind="interval",spec={"seconds":60})
    before=store.get_trigger(t["id"])
    store.set_automation_state(a["id"],"paused")
    paused_child=store.get_trigger(t["id"])
    assert paused_child["state"]=="active" and paused_child["version"]==before["version"]
    store.set_automation_state(a["id"],"active")
    assert store.get_trigger(t["id"])["state"]=="active"


def test_paused_trigger_edit_stays_paused_until_explicit_resume(state_dir):
    store=Store(state_dir)
    a=store.create_automation(name="edit-paused",scope="operator",target_kind="gateway",target_ref=None,action_class="read_local",wake={})
    t=store.create_trigger(automation_id=a["id"],kind="interval",spec={"seconds":60})
    store.set_trigger_state(t["id"],"paused")
    edited=store.update_trigger(t["id"],{"seconds":120})
    assert edited["state"]=="paused" and edited["next_run_at"] is None
    resumed=store.set_trigger_state(t["id"],"active")
    assert resumed["state"]=="active" and resumed["next_run_at"] is not None


def test_target_config_rejects_inline_credentials(state_dir):
    from aiverse_automations.config import validate_target_config
    with pytest.raises(ValidationError):
        validate_target_config("gateway",{"url":"https://example.com/run","bearer_token":"secret"})
    with pytest.raises(ValidationError):
        validate_target_config("gateway",{"url":"https://user:pass@example.com/run"})
    accepted=validate_target_config("gateway",{"url":"https://example.com/run","bearer_token_ref":"env:GATEWAY_TOKEN"})
    assert accepted["bearer_token_ref"]=="env:GATEWAY_TOKEN"


def test_run_now_can_execute_with_paused_trigger_but_not_paused_automation(state_dir,allow_os,owner_server):
    url,received,_=owner_server; _target(state_dir,"gateway",{"url":url})
    store=Store(state_dir); a=store.create_automation(name="manual",scope="operator",target_kind="gateway",target_ref=None,action_class="read_local",wake={})
    t=store.create_trigger(automation_id=a["id"],kind="interval",spec={"seconds":60})
    store.set_trigger_state(t["id"],"paused")
    assert Engine(state_dir).run_now(a["id"])["status"]=="succeeded"
    assert len(received)==1
    store.set_automation_state(a["id"],"paused")
    with pytest.raises(StateConflict): Engine(state_dir).run_now(a["id"])


def test_component_disable_is_kill_fence_for_claimed_run(state_dir,allow_os,owner_server):
    from aiverse_automations.lifecycle import set_enabled
    url,received,_=owner_server; _target(state_dir,"gateway",{"url":url})
    store=Store(state_dir); a=store.create_automation(name="disable-fence",scope="operator",target_kind="gateway",target_ref=None,action_class="read_local",wake={})
    store.create_trigger(automation_id=a["id"],kind="once",spec={"at":"2026-01-01T00:00:00Z"})
    engine=Engine(state_dir); run_id=engine._claim_due(datetime(2026,1,1,0,0,1,tzinfo=timezone.utc))[0]
    set_enabled(state_dir,False)
    result=engine.execute_run(run_id)
    assert result["status"]=="canceled" and result["last_error_code"]=="COMPONENT_DISABLED"
    assert received==[]


def test_invalid_scope_payload_and_target_contracts_fail_early(state_dir):
    store=Store(state_dir)
    with pytest.raises(ValidationError):
        store.create_automation(name="bad",scope="workspace:../x",target_kind="gateway",target_ref=None,action_class="read_local",wake={})
    with pytest.raises(ValidationError):
        store.create_automation(name="bot",scope="operator",target_kind="bot",target_ref="bot1",action_class="read_local",wake={"objective":"x"})
    with pytest.raises(ValidationError):
        store.create_automation(name="badwake",scope="operator",target_kind="gateway",target_ref=None,action_class="read_local",wake=[1,2,3])  # type: ignore[arg-type]
