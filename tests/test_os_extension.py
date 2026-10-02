from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from aiverse_automations.lifecycle import descriptor, doctor, set_enabled, setup, uninstall
from aiverse_automations.os_extension import (
    EXTENSION_ENGINE,
    EXTENSION_ID,
    EXTENSION_INSTRUCTIONS,
    REGISTRY_LOCK_PATH,
    REGISTRY_PATH,
    OsExtensionError,
    install_os_extension,
)
from aiverse_automations.store import Store


def _full_os(tmp_path: Path) -> Path:
    root=tmp_path/"os"
    (root/"scripts").mkdir(parents=True)
    (root/"operator").mkdir()
    (root/"workspaces").mkdir()
    (root/"system"/"extensions").mkdir(parents=True)
    (root/"AI-VERSE.yaml").write_text(
        'schema_version: "2.0"\narchitecture: unified-workspace\n',
        encoding="utf-8",
    )
    (root/"scripts"/"action-permission.mjs").write_text("// fixture\n",encoding="utf-8")
    (root/"AGENTS.md").write_text(".aiverse/extensions/registry.json\n",encoding="utf-8")
    (root/"system"/"extensions"/"README.md").write_text(
        ".aiverse/extensions/registry.json\n",
        encoding="utf-8",
    )
    return root


def _registry(root: Path) -> dict:
    return json.loads((root/REGISTRY_PATH).read_text(encoding="utf-8"))


def test_os_extension_preserves_other_entries_and_executes_atomic_owner(tmp_path: Path):
    root=_full_os(tmp_path)
    state=tmp_path/"automations-state"
    report=setup(state,os_root=str(root),enable=True)
    assert report["state"]=="ready"

    registry=root/REGISTRY_PATH
    registry.parent.mkdir(parents=True,exist_ok=True)
    registry.write_text(json.dumps({
        "schema_version":"1.0",
        "custom_top_level":{"preserve":True},
        "extensions":{
            "other-extension":{"id":"other-extension","custom":"keep"}
        },
    },indent=2)+"\n",encoding="utf-8")

    manifest_before=(root/"AI-VERSE.yaml").read_bytes()
    agents_before=(root/"AGENTS.md").read_bytes()

    attached=install_os_extension(state,root)
    assert attached["status"]=="installed"
    doc=_registry(root)
    assert doc["custom_top_level"]=={"preserve":True}
    assert doc["extensions"]["other-extension"]=={"id":"other-extension","custom":"keep"}
    entry=doc["extensions"][EXTENSION_ID]
    assert entry["id"]==EXTENSION_ID
    assert entry["installed"] is True
    assert entry["supported"] is True
    assert entry["enabled"] is True
    assert entry["engine"]==EXTENSION_ENGINE
    assert entry["state_dir_digest"].startswith("sha256:")
    assert (root/"AI-VERSE.yaml").read_bytes()==manifest_before
    assert (root/"AGENTS.md").read_bytes()==agents_before

    definition={
        "name":"Monday delivery review",
        "scope":"workspace:alpha",
        "target_kind":"gateway",
        "target_ref":None,
        "action_class":"read_local",
        "wake":{"objective":"Review Client Alpha delivery checklist."},
        "trigger":{"kind":"cron","spec":{"expr":"0 9 * * MON","timezone":"Europe/Bucharest"}},
        "idempotency_key":"gateway:run-acceptance:call-automation",
    }
    engine=root/EXTENSION_ENGINE
    first=subprocess.run(
        [sys.executable,str(engine)],
        input=json.dumps({"operation":"create_definition","definition":definition}),
        text=True,capture_output=True,check=True,
    )
    created=json.loads(first.stdout)
    assert created["state"]=="created"
    assert created["automation"]["scope"]=="workspace:alpha"
    assert created["trigger"]["kind"]=="cron"

    second=subprocess.run(
        [sys.executable,str(engine)],
        input=json.dumps({"operation":"create_definition","definition":definition}),
        text=True,capture_output=True,check=True,
    )
    replay=json.loads(second.stdout)
    assert replay["state"]=="existing"
    assert replay["automation"]["id"]==created["automation"]["id"]
    assert replay["trigger"]["id"]==created["trigger"]["id"]
    assert len(Store(state).list_automations())==1

    unchanged=install_os_extension(state,root)
    assert unchanged["status"]=="unchanged"


def test_os_extension_never_steals_existing_registry_lock(tmp_path: Path):
    root=_full_os(tmp_path)
    state=tmp_path/"automations-state"
    setup(state,os_root=str(root),enable=True)
    lock=root/REGISTRY_LOCK_PATH
    lock.parent.mkdir(parents=True,exist_ok=True)
    lock.write_text("owned-by-other-installer\n",encoding="utf-8")

    with pytest.raises(OsExtensionError,match="registry is busy"):
        install_os_extension(state,root)

    assert lock.read_text(encoding="utf-8")=="owned-by-other-installer\n"
    assert not (root/EXTENSION_ENGINE).exists()


def test_os_extension_attachment_reconciles_to_component_enabled_truth(tmp_path: Path):
    root=_full_os(tmp_path)
    state=tmp_path/"automations-state"
    setup(state,os_root=str(root),enable=True)
    first=install_os_extension(state,root)
    assert first["status"]=="installed"

    doc=_registry(root)
    doc["extensions"][EXTENSION_ID]["enabled"]=False
    (root/REGISTRY_PATH).write_text(json.dumps(doc,indent=2)+"\n",encoding="utf-8")

    assert descriptor(state)["state"]=="unhealthy"
    assert descriptor(state)["attachment"]["consistent"] is False

    second=install_os_extension(state,root)
    assert second["status"]=="installed"
    assert _registry(root)["extensions"][EXTENSION_ID]["enabled"] is True
    assert descriptor(state)["state"]=="ready"



def test_attached_enable_disable_uninstall_and_reinstall_stay_synchronized(tmp_path: Path):
    root=_full_os(tmp_path)
    state=tmp_path/"automations-state"
    setup(state,os_root=str(root),enable=True)
    install_os_extension(state,root)

    assert descriptor(state)["state"]=="ready"
    assert descriptor(state)["attachment"]["attached"] is True
    assert _registry(root)["extensions"][EXTENSION_ID]["enabled"] is True

    disabled=set_enabled(state,False)
    assert disabled["state"]=="disabled"
    assert disabled["attachment"]["attached"] is True
    assert disabled["attachment"]["enabled"] is False
    assert disabled["attachment"]["consistent"] is True
    assert _registry(root)["extensions"][EXTENSION_ID]["enabled"] is False

    enabled=set_enabled(state,True)
    assert enabled["state"]=="ready"
    assert enabled["attachment"]["enabled"] is True
    assert enabled["attachment"]["consistent"] is True
    assert _registry(root)["extensions"][EXTENSION_ID]["enabled"] is True

    database=Path(descriptor(state)["database"])
    assert database.is_file()
    result=uninstall(state)
    assert result["state"]=="setup-required"
    assert result["uninstall_preserves_canonical_state"] is True
    assert database.is_file()
    assert EXTENSION_ID not in _registry(root)["extensions"]
    assert not (root/EXTENSION_ENGINE).exists()
    assert not (root/EXTENSION_INSTRUCTIONS).exists()
    assert result["detached_extension"]["status"]=="detached"

    setup_result=setup(state,os_root=str(root),enable=True)
    assert setup_result["state"]=="ready"
    assert setup_result["attachment"]["attached"] is False
    reattached=install_os_extension(state,root)
    assert reattached["status"]=="installed"
    assert _registry(root)["extensions"][EXTENSION_ID]["enabled"] is True
    assert (root/EXTENSION_ENGINE).is_file()
    assert (root/EXTENSION_INSTRUCTIONS).is_file()
    assert descriptor(state)["attachment"]["consistent"] is True


def test_lifecycle_rolls_back_component_state_if_registry_lock_is_busy(tmp_path: Path):
    root=_full_os(tmp_path)
    state=tmp_path/"automations-state"
    setup(state,os_root=str(root),enable=True)
    install_os_extension(state,root)

    lock=root/REGISTRY_LOCK_PATH
    lock.write_text("owned-by-other-installer\n",encoding="utf-8")
    try:
        with pytest.raises(OsExtensionError,match="registry is busy"):
            set_enabled(state,False)
    finally:
        lock.unlink()

    status=descriptor(state)
    assert status["state"]=="ready"
    assert status["enabled"] is True
    assert status["attachment"]["enabled"] is True
    assert status["attachment"]["consistent"] is True


def test_doctor_exposes_manual_attachment_lifecycle_divergence(tmp_path: Path):
    root=_full_os(tmp_path)
    state=tmp_path/"automations-state"
    setup(state,os_root=str(root),enable=True)
    install_os_extension(state,root)

    doc=_registry(root)
    doc["extensions"][EXTENSION_ID]["enabled"]=False
    (root/REGISTRY_PATH).write_text(json.dumps(doc,indent=2)+"\n",encoding="utf-8")

    status=descriptor(state)
    assert status["state"]=="unhealthy"
    assert status["attachment"]["consistent"] is False

    diagnosis=doctor(state)
    assert diagnosis["state"]=="unhealthy"
    assert diagnosis["ok"] is False
    check=next(c for c in diagnosis["checks"] if c["name"]=="os-extension-attachment")
    assert check["ok"] is False
    assert check["detail"]["attached"] is True
    assert check["detail"]["enabled"] is False
