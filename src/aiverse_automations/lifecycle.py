from __future__ import annotations

from pathlib import Path

from . import COMPONENT_ID, __version__
from .config import config_path, default_config, load_config, save_config
from .db import db_path, initialize, integrity_check, connect
from .errors import ValidationError
from .os_extension import detach_os_extension, inspect_os_extension, sync_os_extension_enabled
from .util import utc_now

LIFECYCLE_COMMANDS=["install","setup","status","doctor","enable","disable","update","uninstall"]


def _permission_boundary(os_root: str | None) -> Path | None:
    return Path(os_root,"scripts","action-permission.mjs") if os_root else None


def _os_dependency_ok(os_root: str | None) -> bool:
    permission=_permission_boundary(os_root)
    return bool(permission and permission.is_file() and not permission.is_symlink())


def legacy_definition_paths(os_root: str | None) -> list[str]:
    if not os_root:
        return []
    root=Path(os_root)
    if not root.is_dir() or root.is_symlink():
        return []
    candidates=[]
    roots=[root/"automations"/"jobs",root/"automations"/"triggers"]
    workspaces=root/"workspaces"
    if workspaces.is_dir() and not workspaces.is_symlink():
        for workspace in workspaces.iterdir():
            if workspace.is_dir() and not workspace.is_symlink():
                roots.append(workspace/"automations")
    for base in roots:
        if not base.is_dir() or base.is_symlink():
            continue
        for item in base.rglob("*"):
            if not item.is_file() or item.is_symlink():
                continue
            rel=item.relative_to(root).as_posix()
            if item.name.lower()=="readme.md" or "/.ai-verse-automations/" in f"/{rel}":
                continue
            candidates.append(rel)
    return sorted(set(candidates))


def migration_authority(state_dir: Path, *, config: dict | None = None) -> dict:
    config=config or load_config(state_dir,required=False)
    configured=sorted(set(config.get("migration_sources") or []))
    discovered=legacy_definition_paths(config.get("os_root"))
    sources=sorted(set(configured+discovered))
    required=bool(config.get("migration_required") or sources)
    return {
      "required":required,
      "configured":configured,
      "discovered":discovered,
      "sources":sources,
    }


def attachment_authority(state_dir: Path, *, config: dict | None = None) -> dict:
    config=config or load_config(state_dir,required=False)
    os_root=config.get("os_root")
    if not isinstance(os_root,str) or not os_root:
        return {
          "attached":False,"enabled":None,"installed":False,
          "consistent":True,"files":{"engine":False,"instructions":False},
          "registry":None,
        }
    try:
        return inspect_os_extension(state_dir,Path(os_root))
    except Exception as exc:
        return {
          "attached":None,"enabled":None,"installed":None,
          "consistent":False,"files":{"engine":False,"instructions":False},
          "registry":None,"error":str(exc),
        }


def _sync_attachment_enabled(state_dir: Path, config: dict, enabled: bool) -> dict:
    os_root=config.get("os_root")
    if not isinstance(os_root,str) or not os_root:
        return {"status":"detached","enabled":None,"registry_written":False}
    attachment=attachment_authority(state_dir,config=config)
    if attachment.get("attached") is False and attachment.get("consistent") is True:
        return {"status":"detached","enabled":None,"registry_written":False}
    if attachment.get("attached") is not True:
        raise ValidationError(
            "OS extension attachment is inconsistent and cannot be synchronized safely"
        )
    return sync_os_extension_enabled(state_dir,Path(os_root),enabled)


def _detach_attachment(state_dir: Path, config: dict) -> dict:
    os_root=config.get("os_root")
    if not isinstance(os_root,str) or not os_root:
        return {"status":"unchanged","registry_written":False,"removed_files":[]}
    attachment=attachment_authority(state_dir,config=config)
    if attachment.get("attached") is False and attachment.get("consistent") is True:
        return {"status":"unchanged","registry_written":False,"removed_files":[]}
    if attachment.get("attached") not in {True,False}:
        raise ValidationError(
            "OS extension attachment is inconsistent and cannot be detached safely"
        )
    return detach_os_extension(state_dir,Path(os_root))


def descriptor(state_dir: Path) -> dict:
    config=load_config(state_dir,required=False)
    exists=config_path(state_dir).exists(); database=db_path(state_dir).exists()
    migration=migration_authority(state_dir,config=config)
    migration_sources=migration["sources"]
    migration_required=migration["required"]
    attachment=attachment_authority(state_dir,config=config)
    attachment_ok=bool(attachment.get("consistent"))
    db_ok=False
    if database:
        db_ok,_=integrity_check(state_dir)
    dependency_ok=_os_dependency_ok(config.get("os_root")) if exists else False
    if not exists or not config.get("setup_complete"):
        state="setup-required"
    elif migration_required:
        state="migration-required"
    elif not attachment_ok:
        state="unhealthy"
    elif not config.get("enabled"):
        state="disabled"
    elif not db_ok or not dependency_ok:
        state="unhealthy"
    else:
        state="ready"
    return {
      "component_id":COMPONENT_ID,"version":__version__,
      "compatibility":{"host":"ai-verse-os >= public-beta-contract-2026-09-13"},
      "required_host_version":"public-beta-contract-2026-09-13",
      "package_source":"python-package","state":state,"setup_required":state=="setup-required",
      "supported_lifecycle_commands":LIFECYCLE_COMMANDS,
      "requested_scopes":["operator","workspace:*"],
      "requested_capabilities":["schedule","event_ingress","owner_wake_delivery"],
      "authority_transfer_separate":True,"uninstall_preserves_canonical_state":True,
      "migration_required":migration_required,"migration_sources":migration_sources,
      "migration_sources_configured":migration["configured"],"migration_sources_discovered":migration["discovered"],
      "attachment":attachment,
      "health":{"database":db_ok,"os_permission_boundary":dependency_ok,"os_extension_attachment":attachment_ok},
      "state_dir":str(state_dir),"database":str(db_path(state_dir)),"enabled":bool(config.get("enabled")),"os_root":config.get("os_root")
    }


def setup(state_dir: Path, *, os_root: str | None = None, enable: bool = True) -> dict:
    if not os_root:
        raise ValidationError("setup requires --os-root so every wake can re-check OS scope/permission")
    requested=Path(os_root).expanduser()
    if requested.is_symlink():
        raise ValidationError("--os-root may not be a symlink")
    resolved=requested.resolve()
    if not resolved.is_dir():
        raise ValidationError("--os-root must be a regular AI-Verse OS directory")
    permission=resolved/"scripts"/"action-permission.mjs"
    if permission.is_symlink() or not permission.is_file():
        raise ValidationError("AI-Verse OS action-permission boundary is missing")
    state_dir.mkdir(parents=True,exist_ok=True); initialize(state_dir)
    config=load_config(state_dir,required=False); base=default_config(); base.update(config)
    legacy=legacy_definition_paths(str(resolved))
    base["setup_complete"]=True; base["uninstalled"]=False
    base["os_root"]=str(resolved)
    base["migration_required"]=bool(legacy); base["migration_sources"]=legacy
    # Never enable two competing definition authorities silently.
    base["enabled"]=bool(enable and not legacy)
    previous=dict(config)
    save_config(state_dir,base)
    try:
        _sync_attachment_enabled(state_dir,base,bool(base["enabled"]))
    except Exception:
        if config_path(state_dir).exists():
            if previous:
                save_config(state_dir,previous)
            else:
                config_path(state_dir).unlink(missing_ok=True)
        raise
    return descriptor(state_dir)


def set_enabled(state_dir: Path, enabled: bool) -> dict:
    config=load_config(state_dir,required=True)
    migration=migration_authority(state_dir,config=config)
    if enabled and migration["required"]:
        raise ValidationError(
            "cannot enable while legacy OS automation definitions require migration: "
            + ", ".join(migration["sources"])
        )
    previous=dict(config)
    config["enabled"]=enabled; config["uninstalled"]=False
    save_config(state_dir,config)
    try:
        _sync_attachment_enabled(state_dir,config,enabled)
    except Exception:
        save_config(state_dir,previous)
        raise
    return descriptor(state_dir)


def update(state_dir: Path) -> dict:
    config=load_config(state_dir,required=True); before=bool(config.get("enabled")); initialize(state_dir); config["enabled"]=before; save_config(state_dir,config); return descriptor(state_dir)


def uninstall(state_dir: Path) -> dict:
    config=load_config(state_dir,required=True)
    previous=dict(config)
    config["enabled"]=False
    save_config(state_dir,config)
    detach={"status":"unchanged","registry_written":False,"removed_files":[]}
    try:
        detach=_detach_attachment(state_dir,config)
    except Exception:
        save_config(state_dir,previous)
        raise
    config["uninstalled"]=True
    config["setup_complete"]=False
    save_config(state_dir,config)
    return {
      **descriptor(state_dir),
      "preserved_state":str(db_path(state_dir)),
      "detached_extension":detach,
      "note":"canonical automation definitions/runs were preserved",
    }


def doctor(state_dir: Path) -> dict:
    checks=[]; config=load_config(state_dir,required=False); status=descriptor(state_dir)
    checks.append({"depth":"structural","name":"config","ok":config_path(state_dir).is_file(),"detail":str(config_path(state_dir))})
    if db_path(state_dir).exists():
        ok,detail=integrity_check(state_dir); checks.append({"depth":"runtime","name":"sqlite-integrity","ok":ok,"detail":detail})
        if ok:
            conn=connect(state_dir)
            try:
                overdue=conn.execute("SELECT COUNT(*) FROM triggers WHERE state='active' AND next_run_at IS NOT NULL AND next_run_at<?",(utc_now(),)).fetchone()[0]
                stale=conn.execute("SELECT COUNT(*) FROM runs WHERE status IN ('claimed','authorizing','delivering')").fetchone()[0]
                dead=conn.execute("SELECT COUNT(*) FROM runs WHERE status IN ('dead_letter','unknown')").fetchone()[0]
            finally: conn.close()
            checks.append({"depth":"operational","name":"overdue-triggers","ok":True,"detail":int(overdue)})
            checks.append({"depth":"operational","name":"nonterminal-runs","ok":True,"detail":int(stale)})
            checks.append({"depth":"operational","name":"recovery-attention","ok":True,"detail":int(dead)})
        else:
            checks.append({"depth":"operational","name":"store-operational","ok":False,"detail":"skipped because canonical store identity/schema is invalid"})
    else:
        checks.append({"depth":"runtime","name":"sqlite-integrity","ok":False,"detail":"database missing"})
    os_root=config.get("os_root")
    permission=_permission_boundary(os_root)
    checks.append({"depth":"dependency","name":"os-permission-boundary","ok":_os_dependency_ok(os_root),"detail":str(permission) if permission else "os_root not configured"})
    attachment=status["attachment"]
    checks.append({
      "depth":"attachment/discovery",
      "name":"os-extension-attachment",
      "ok":bool(attachment.get("consistent")),
      "detail":attachment,
    })
    migration={
      "required":status["migration_required"],
      "configured":status["migration_sources_configured"],
      "discovered":status["migration_sources_discovered"],
    }
    checks.append({"depth":"attachment/discovery","name":"legacy-definition-handoff","ok":not migration["required"],"detail":{"configured":migration["configured"],"discovered":migration["discovered"]}})
    critical={"config","sqlite-integrity","os-permission-boundary","os-extension-attachment","legacy-definition-handoff"}
    return {"component_id":COMPONENT_ID,"version":__version__,"checked_depths":["structural","attachment/discovery","runtime","dependency","operational"],"ok":all(c["ok"] for c in checks if c["name"] in critical),"checks":checks,"state":status["state"]}
