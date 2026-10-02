from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiverse_automations.config import load_config, save_config
from aiverse_automations.engine import Engine
from aiverse_automations.errors import StateConflict, ValidationError
from aiverse_automations.lifecycle import descriptor, doctor, set_enabled
from aiverse_automations.store import Store


def _legacy_path(state_dir: Path) -> Path:
    config=load_config(state_dir,required=True)
    root=Path(config["os_root"])
    path=root/"automations"/"jobs"/"post-setup-legacy.yaml"
    path.parent.mkdir(parents=True,exist_ok=True)
    return path


def _target(state_dir: Path, url: str) -> None:
    config=load_config(state_dir,required=True)
    config["targets"]["gateway"]={"url":url}
    save_config(state_dir,config)


def _due_definition(state_dir: Path) -> tuple[Store, dict, dict]:
    store=Store(state_dir)
    automation=store.create_automation(
        name="legacy-fence",
        scope="operator",
        target_kind="gateway",
        target_ref=None,
        action_class="read_local",
        wake={"objective":"must not run beside legacy authority"},
    )
    trigger=store.create_trigger(
        automation_id=automation["id"],
        kind="once",
        spec={"at":"2026-01-01T00:00:00Z"},
    )
    return store,automation,trigger


def test_post_setup_legacy_conflict_makes_status_and_doctor_migration_required(state_dir: Path):
    assert descriptor(state_dir)["state"]=="ready"
    path=_legacy_path(state_dir)
    path.write_text("schedule: daily\n",encoding="utf-8")

    status=descriptor(state_dir)
    assert status["state"]=="migration-required"
    assert status["migration_required"] is True
    assert status["migration_sources_discovered"]==["automations/jobs/post-setup-legacy.yaml"]
    assert status["migration_sources"]==["automations/jobs/post-setup-legacy.yaml"]

    diagnosis=doctor(state_dir)
    assert diagnosis["state"]=="migration-required"
    assert diagnosis["ok"] is False
    handoff=next(check for check in diagnosis["checks"] if check["name"]=="legacy-definition-handoff")
    assert handoff["ok"] is False
    assert handoff["detail"]["discovered"]==["automations/jobs/post-setup-legacy.yaml"]

    with pytest.raises(ValidationError,match="legacy OS automation definitions require migration"):
        set_enabled(state_dir,True)

    path.unlink()
    assert descriptor(state_dir)["state"]=="ready"
    assert doctor(state_dir)["state"]=="ready"
    assert doctor(state_dir)["ok"] is True


def test_tick_does_not_claim_or_advance_due_work_while_live_legacy_conflict_exists(
    state_dir: Path,
    allow_os,
    owner_server,
):
    url,received,_=owner_server
    _target(state_dir,url)
    store,automation,trigger=_due_definition(state_dir)
    engine=Engine(state_dir)
    now=datetime(2026,1,1,0,0,1,tzinfo=timezone.utc)

    before=store.get_trigger(trigger["id"])
    path=_legacy_path(state_dir)
    path.write_text("schedule: daily\n",encoding="utf-8")

    assert engine.tick(now=now)==[]
    assert store.runs(automation["id"])==[]
    after=store.get_trigger(trigger["id"])
    assert after["state"]==before["state"]=="active"
    assert after["next_run_at"]==before["next_run_at"]
    assert received==[]

    path.unlink()
    result=engine.tick(now=now)
    assert len(result)==1
    assert result[0]["status"]=="succeeded"
    assert len(received)==1


def test_claimed_run_is_blocked_and_recoverable_if_legacy_conflict_appears_before_execute(
    state_dir: Path,
    allow_os,
    owner_server,
):
    url,received,_=owner_server
    _target(state_dir,url)
    _,_,_=_due_definition(state_dir)
    engine=Engine(state_dir)
    run_id=engine._claim_due(datetime(2026,1,1,0,0,1,tzinfo=timezone.utc))[0]

    path=_legacy_path(state_dir)
    path.write_text("schedule: daily\n",encoding="utf-8")

    result=engine.execute_run(run_id)
    assert result["status"]=="blocked"
    assert result["last_error_code"]=="LEGACY_AUTHORITY_CONFLICT"
    assert "post-setup-legacy.yaml" in result["last_error"]
    assert received==[]

    path.unlink()
    recovered=engine.retry_run(run_id)
    assert recovered["status"]=="succeeded"
    assert len(received)==1


def test_final_delivery_edge_rechecks_live_legacy_authority(
    state_dir: Path,
    owner_server,
    monkeypatch,
):
    url,received,_=owner_server
    _target(state_dir,url)
    _,_,_=_due_definition(state_dir)
    engine=Engine(state_dir)
    run_id=engine._claim_due(datetime(2026,1,1,0,0,1,tzinfo=timezone.utc))[0]
    path=_legacy_path(state_dir)

    def allow_then_inject(*, os_root, request, timeout_seconds=10.0):
        path.write_text("schedule: daily\n",encoding="utf-8")
        return {**request,"decision":"allow","source":"test"}

    monkeypatch.setattr("aiverse_automations.engine.os_permission",allow_then_inject)
    result=engine.execute_run(run_id)

    assert result["status"]=="blocked"
    assert result["last_error_code"]=="LEGACY_AUTHORITY_CONFLICT"
    assert received==[]


def test_manual_and_event_ingress_fail_before_run_creation_during_live_conflict(
    state_dir: Path,
):
    store=Store(state_dir)
    automation=store.create_automation(
        name="manual-fence",
        scope="operator",
        target_kind="gateway",
        target_ref=None,
        action_class="read_local",
        wake={},
    )
    manual_trigger=store.create_trigger(
        automation_id=automation["id"],
        kind="interval",
        spec={"seconds":60},
    )
    event_automation=store.create_automation(
        name="event-fence",
        scope="operator",
        target_kind="gateway",
        target_ref=None,
        action_class="read_local",
        wake={},
    )
    event_trigger=store.create_trigger(
        automation_id=event_automation["id"],
        kind="event",
        spec={"source":"calendar","event_type":"changed"},
    )
    path=_legacy_path(state_dir)
    path.write_text("schedule: daily\n",encoding="utf-8")

    engine=Engine(state_dir)
    with pytest.raises(StateConflict,match="legacy OS automation definitions require migration"):
        engine.run_now(automation["id"])
    with pytest.raises(StateConflict,match="legacy OS automation definitions require migration"):
        engine.ingest_event(
            trigger_id=event_trigger["id"],
            event_id="evt-live-conflict",
            event={"x":1},
            source_kind="event",
            source="calendar",
            event_type="changed",
        )

    assert store.runs(automation["id"])==[]
    assert store.runs(event_automation["id"])==[]
