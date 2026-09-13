from __future__ import annotations

from pathlib import Path

import pytest

from aiverse_automations.config import load_config, save_config
from aiverse_automations.engine import Engine
from aiverse_automations.errors import StateConflict
from aiverse_automations.store import Store


def test_event_replay_is_idempotent_and_drift_rejected(state_dir,allow_os,owner_server):
    url,received,_=owner_server
    cfg=load_config(state_dir,required=True); cfg["targets"]["gateway"]={"url":url}; save_config(state_dir,cfg)
    store=Store(state_dir); a=store.create_automation(name="event",scope="operator",target_kind="gateway",target_ref=None,action_class="read_local",wake={})
    t=store.create_trigger(automation_id=a["id"],kind="event",spec={"source":"local-test","event_type":"changed"})
    engine=Engine(state_dir)
    first=engine.ingest_event(trigger_id=t["id"],event_id="evt-1",event={"x":1},source_kind="event",source="local-test",event_type="changed")
    second=engine.ingest_event(trigger_id=t["id"],event_id="evt-1",event={"x":1},source_kind="event",source="local-test",event_type="changed")
    assert first["id"]==second["id"]
    assert len(received)==1
    with pytest.raises(StateConflict):
        engine.ingest_event(trigger_id=t["id"],event_id="evt-1",event={"x":2},source_kind="event",source="local-test",event_type="changed")
