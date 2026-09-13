from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import ValidationError
from .util import atomic_write_json

SCHEMA_VERSION = "1.0"


def default_state_dir() -> Path:
    override = os.getenv("AI_VERSE_AUTOMATIONS_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".ai-verse" / "automations").resolve()


def config_path(state_dir: Path) -> Path:
    return state_dir / "component.json"


def default_config() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": False,
        "setup_complete": False,
        "uninstalled": False,
        "os_root": None,
        "migration_required": False,
        "migration_sources": [],
        "tick_seconds": 15,
        "claim_timeout_seconds": 300,
        "http_timeout_seconds": 30,
        "webhook": {"host": "127.0.0.1", "port": 8766, "max_body_bytes": 65536},
        "targets": {},
    }


def _secret_ref(value: Any, label: str) -> str:
    if not isinstance(value,str) or not (value.startswith("env:") or value.startswith("file:")):
        raise ValidationError(f"{label} must be an env: or file: secret reference")
    return value


def validate_target_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config,dict):
        raise ValidationError("target configuration must be an object")
    allowed = {
        "brain": {"root","vendor","binary","host_adapter","read_only_context","model","provider"},
        "bot": {"url","protocol","bearer_token_ref"},
        "team_run": {"url","protocol","bearer_token_ref"},
        "gateway": {"url","bearer_token_ref"},
    }
    if kind not in allowed:
        raise ValidationError(f"unsupported target kind: {kind}")
    unknown=set(config)-allowed[kind]
    if unknown:
        raise ValidationError(f"unknown {kind} target fields: {', '.join(sorted(unknown))}")
    out=dict(config)
    if "bearer_token_ref" in out:
        _secret_ref(out["bearer_token_ref"],"bearer_token_ref")
    if kind in {"bot","team_run","gateway"}:
        url=out.get("url")
        if not isinstance(url,str):
            raise ValidationError(f"{kind} target requires url")
        parsed=urlparse(url)
        if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValidationError("target url must be credential-free http(s)")
        if parsed.scheme=="http" and parsed.hostname not in {"127.0.0.1","localhost","::1"}:
            raise ValidationError("plain HTTP target urls must be loopback-only")
    if kind in {"bot","team_run"}:
        protocol=out.get("protocol","ai-verse-multiple-bots-v1")
        if protocol!="ai-verse-multiple-bots-v1":
            raise ValidationError("unsupported Multiple Bots automation protocol")
        out["protocol"]=protocol
    if kind=="brain":
        if not isinstance(out.get("root"),str) or out.get("vendor") not in {"claude","codex","hermes"}:
            raise ValidationError("brain target requires root and vendor")
        if bool(out.get("host_adapter")) == bool(out.get("read_only_context")):
            raise ValidationError("brain target requires exactly one of host_adapter or read_only_context=true")
    return out


def validate_config(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError("unsupported component config schema_version")
    targets=data.get("targets",{})
    if not isinstance(targets,dict):
        raise ValidationError("targets must be an object")
    normalized={}
    for kind,cfg in targets.items():
        normalized[kind]=validate_target_config(kind,cfg)
    data=dict(data); data["targets"]=normalized
    webhook=data.get("webhook",{})
    if not isinstance(webhook,dict) or set(webhook)-{"host","port","max_body_bytes"}:
        raise ValidationError("invalid webhook server config")
    if webhook.get("host","127.0.0.1") not in {"127.0.0.1","localhost","::1"}:
        raise ValidationError("public-beta webhook/control server is loopback-only")
    port=webhook.get("port",8766); max_body=webhook.get("max_body_bytes",65536)
    if not isinstance(port,int) or not 0 <= port <= 65535:
        raise ValidationError("invalid webhook port")
    if not isinstance(max_body,int) or not 1024 <= max_body <= 1048576:
        raise ValidationError("invalid webhook body bound")
    tick=data.get("tick_seconds",15); claim=data.get("claim_timeout_seconds",300); timeout=data.get("http_timeout_seconds",30)
    if not isinstance(tick,int) or not 1 <= tick <= 3600:
        raise ValidationError("tick_seconds must be 1..3600")
    if not isinstance(timeout,(int,float)) or not 1 <= timeout <= 300:
        raise ValidationError("http_timeout_seconds must be 1..300")
    if not isinstance(claim,int) or claim < int(timeout)+30 or claim > 86400:
        raise ValidationError("claim_timeout_seconds must exceed owner timeout by at least 30 seconds")
    return data


def load_config(state_dir: Path, *, required: bool = False) -> dict[str, Any]:
    path = config_path(state_dir)
    if not path.exists():
        if required:
            raise ValidationError(f"component is not setup: {path} does not exist")
        return default_config()
    data = json.loads(path.read_text(encoding="utf-8"))
    return validate_config(data)


def save_config(state_dir: Path, data: dict[str, Any]) -> None:
    data = dict(data)
    data["schema_version"] = SCHEMA_VERSION
    data=validate_config(data)
    atomic_write_json(config_path(state_dir), data)
