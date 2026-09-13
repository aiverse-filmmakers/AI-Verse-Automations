from pathlib import Path

from aiverse_automations.lifecycle import descriptor, doctor, set_enabled, uninstall, update


def test_lifecycle_preserves_state(state_dir: Path):
    assert descriptor(state_dir)["state"]=="ready"
    assert set_enabled(state_dir,False)["state"]=="disabled"
    assert set_enabled(state_dir,True)["state"]=="ready"
    assert update(state_dir)["state"]=="ready"
    report=doctor(state_dir)
    assert report["checked_depths"]==["structural","attachment/discovery","runtime","dependency","operational"]
    assert report["ok"] is True
    result=uninstall(state_dir)
    assert result["uninstall_preserves_canonical_state"] is True
    assert Path(result["preserved_state"]).exists()
