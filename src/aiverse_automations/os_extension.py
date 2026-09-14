from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import __version__
from .config import load_config
from .errors import AutomationsError

EXTENSION_ID = "ai-verse-automations"
EXTENSION_SOURCE = "AI-Verse-Automations"
EXTENSION_ROOT = ".aiverse/extensions/ai-verse-automations"
EXTENSION_ENGINE = f"{EXTENSION_ROOT}/engine.py"
EXTENSION_INSTRUCTIONS = f"{EXTENSION_ROOT}/INSTRUCTIONS.md"
REGISTRY_PATH = ".aiverse/extensions/registry.json"
REGISTRY_LOCK_PATH = ".aiverse/extensions/registry.json.lock"
REGISTRY_SCHEMA = "1.0"
MAX_REGISTRY_BYTES = 1024 * 1024


class OsExtensionError(AutomationsError):
    pass


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _relative_path(root: Path, relative: str) -> Path:
    if (
        not relative
        or "\x00" in relative
        or relative.startswith(("/", "\\"))
        or "\\" in relative
    ):
        raise OsExtensionError(f"unsafe extension path: {relative}")
    parts = relative.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise OsExtensionError(f"unsafe extension path: {relative}")
    target = root.joinpath(*parts)
    if not _inside(root, target):
        raise OsExtensionError(f"extension path escapes OS root: {relative}")
    return target


def _assert_no_symlink_chain(root: Path, relative: str, *, include_leaf: bool) -> None:
    parts = relative.split("/")
    current = root
    limit = len(parts) if include_leaf else max(0, len(parts) - 1)
    for part in parts[:limit]:
        current = current / part
        if not current.exists():
            continue
        if current.is_symlink():
            raise OsExtensionError(f"extension path traverses symlink: {relative}")


def _require_bound_owner(state_dir: Path, os_root: Path) -> None:
    config = load_config(state_dir, required=True)
    if config.get("setup_complete") is not True:
        raise OsExtensionError("Automations must be setup before OS attachment")
    configured = config.get("os_root")
    if not isinstance(configured, str) or Path(configured).resolve() != os_root:
        raise OsExtensionError("Automations setup is not bound to this OS root")
    manifest = os_root / "AI-VERSE.yaml"
    if manifest.is_symlink() or not manifest.is_file():
        raise OsExtensionError("AI-Verse OS manifest is missing or unsafe")
    text = manifest.read_text(encoding="utf-8")
    if "schema_version:" not in text:
        raise OsExtensionError("AI-Verse OS manifest has no schema_version")
    if not (os_root / "operator").is_dir() or not (os_root / "workspaces").is_dir():
        raise OsExtensionError("AI-Verse OS v2 layout is incomplete")


def _read_registry(root: Path) -> tuple[dict[str, Any], str | None]:
    path = _relative_path(root, REGISTRY_PATH)
    _assert_no_symlink_chain(root, REGISTRY_PATH, include_leaf=True)
    if not path.exists():
        return {"schema_version": REGISTRY_SCHEMA, "extensions": {}}, None
    if path.is_symlink() or not path.is_file():
        raise OsExtensionError("OS extension registry must be a regular file")
    if path.stat().st_size > MAX_REGISTRY_BYTES:
        raise OsExtensionError("OS extension registry is too large")
    raw = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise OsExtensionError(f"OS extension registry is invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or parsed.get("schema_version") != REGISTRY_SCHEMA:
        raise OsExtensionError("OS extension registry schema is unsupported")
    if not isinstance(parsed.get("extensions"), dict):
        raise OsExtensionError("OS extension registry extensions must be an object")
    return parsed, raw


def _ensure_extension_dir(root: Path) -> Path:
    parent = _relative_path(root, ".aiverse/extensions")
    _assert_no_symlink_chain(root, ".aiverse/extensions", include_leaf=True)
    parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_chain(root, ".aiverse/extensions", include_leaf=True)
    directory = _relative_path(root, EXTENSION_ROOT)
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise OsExtensionError("Automations extension root is unsafe")
    return directory


def _instructions() -> str:
    return """# AI-Verse Automations local owner bridge

This extension exposes only the Automations-owned atomic definition creation path.

- Automations remains canonical for schedules, triggers and wake delivery.
- OS remains authoritative for scope and action permission.
- Gateway supplies trusted user-consent provenance before OS calls this bridge.
- The bridge does not execute schedules, create credentials, widen permissions or become a second scheduler.
- Canonical Automation state remains in the configured Automations state directory.
"""


def _engine(state_dir: Path) -> str:
    encoded_state = json.dumps(str(state_dir.resolve()))
    return f"""#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

from aiverse_automations.lifecycle import descriptor
from aiverse_automations.store import Store

STATE_DIR = Path({encoded_state})


def main() -> int:
    raw = sys.stdin.read()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or set(payload) != {{"operation", "definition"}}:
        raise RuntimeError("Automations owner bridge requires exactly operation and definition")
    if payload["operation"] != "create_definition":
        raise RuntimeError("unsupported Automations owner bridge operation")
    definition = payload["definition"]
    if not isinstance(definition, dict):
        raise RuntimeError("Automation definition must be an object")
    current = descriptor(STATE_DIR)
    if current.get("state") != "ready":
        raise RuntimeError("Automations owner is not ready")
    allowed = {{
        "name", "scope", "target_kind", "target_ref", "action_class",
        "wake", "retry", "trigger", "idempotency_key"
    }}
    required = {{
        "name", "scope", "target_kind", "action_class",
        "wake", "trigger", "idempotency_key"
    }}
    if set(definition) - allowed or required - set(definition):
        raise RuntimeError("Automation definition fields are invalid")
    trigger = definition.get("trigger")
    if not isinstance(trigger, dict) or set(trigger) != {{"kind", "spec"}}:
        raise RuntimeError("Automation trigger must contain exactly kind and spec")
    result = Store(STATE_DIR).create_definition(
        name=definition["name"],
        scope=definition["scope"],
        target_kind=definition["target_kind"],
        target_ref=definition.get("target_ref"),
        action_class=definition["action_class"],
        wake=definition["wake"],
        retry=definition.get("retry"),
        trigger_kind=trigger["kind"],
        trigger_spec=trigger["spec"],
        idempotency_key=definition["idempotency_key"],
    )
    sys.stdout.write(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _write_owned_file(root: Path, relative: str, content: str) -> bool:
    path = _relative_path(root, relative)
    _assert_no_symlink_chain(root, relative, include_leaf=True)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise OsExtensionError(f"extension-owned file is unsafe: {relative}")
        if path.read_text(encoding="utf-8") == content:
            return False
        raise OsExtensionError(f"refusing to overwrite different extension-owned file: {relative}")
    temp = path.with_name(path.name + f".{uuid4().hex}.tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        if os.name != "nt":
            os.chmod(temp, 0o600)
        if path.exists():
            raise OsExtensionError(f"extension-owned file appeared during install: {relative}")
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass
    return True


def _entry(existing: Any, state_dir: Path) -> dict[str, Any]:
    if existing is None:
        current: dict[str, Any] = {}
    elif isinstance(existing, dict):
        current = dict(existing)
        if (
            current.get("id") not in {None, EXTENSION_ID}
            or current.get("source") not in {None, EXTENSION_SOURCE}
        ):
            raise OsExtensionError("existing Automations extension entry is not owned by this component")
    else:
        raise OsExtensionError("existing Automations extension entry is invalid")
    enabled = current.get("enabled", True)
    if not isinstance(enabled, bool):
        raise OsExtensionError("existing Automations extension enabled state is invalid")
    return {
        **current,
        "id": EXTENSION_ID,
        "supported": True,
        "installed": True,
        "enabled": enabled,
        "version": __version__,
        "source": EXTENSION_SOURCE,
        "instructions": EXTENSION_INSTRUCTIONS,
        "engine": EXTENSION_ENGINE,
        "adapters": [],
        "state_dir_digest": "sha256:" + hashlib.sha256(
            str(state_dir.resolve()).encode("utf-8")
        ).hexdigest(),
    }


def _write_registry(root: Path, document: dict[str, Any], expected_raw: str | None) -> None:
    path = _relative_path(root, REGISTRY_PATH)
    current_raw = path.read_text(encoding="utf-8") if path.exists() else None
    if current_raw != expected_raw:
        raise OsExtensionError("OS extension registry changed during Automations attachment")
    temp = path.with_name(path.name + f".{uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass


def install_os_extension(state_dir: Path, os_root: Path) -> dict[str, Any]:
    state_dir = state_dir.expanduser().resolve()
    os_root = os_root.expanduser().resolve()
    _require_bound_owner(state_dir, os_root)
    _ensure_extension_dir(os_root)

    lock = _relative_path(os_root, REGISTRY_LOCK_PATH)
    _assert_no_symlink_chain(os_root, REGISTRY_LOCK_PATH, include_leaf=True)
    fd: int | None = None
    acquired = False
    created: list[str] = []
    try:
        try:
            fd = os.open(str(lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            acquired = True
            os.write(fd, json.dumps({
                "extension_id": EXTENSION_ID,
                "created_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
            }).encode("utf-8"))
            os.close(fd)
            fd = None
        except FileExistsError as exc:
            raise OsExtensionError("OS extension registry is busy") from exc

        document, raw = _read_registry(os_root)
        extensions = dict(document["extensions"])
        next_entry = _entry(extensions.get(EXTENSION_ID), state_dir)

        if _write_owned_file(os_root, EXTENSION_INSTRUCTIONS, _instructions()):
            created.append(EXTENSION_INSTRUCTIONS)
        if _write_owned_file(os_root, EXTENSION_ENGINE, _engine(state_dir)):
            created.append(EXTENSION_ENGINE)

        extensions[EXTENSION_ID] = next_entry
        next_document = {
            **document,
            "schema_version": REGISTRY_SCHEMA,
            "extensions": extensions,
        }
        if document != next_document:
            _write_registry(os_root, next_document, raw)
            registry_written = True
        else:
            registry_written = False

        return {
            "status": "installed" if created or registry_written else "unchanged",
            "extension_id": EXTENSION_ID,
            "version": __version__,
            "engine": EXTENSION_ENGINE,
            "instructions": EXTENSION_INSTRUCTIONS,
            "registry": REGISTRY_PATH,
            "registry_written": registry_written,
            "materialized_files": sorted(created),
            "state_dir_digest": next_entry["state_dir_digest"],
            "tracked_os_files_mutated": [],
        }
    except Exception:
        for relative in reversed(created):
            try:
                path = _relative_path(os_root, relative)
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            except Exception:
                pass
        raise
    finally:
        if fd is not None:
            os.close(fd)
        if acquired:
            try:
                lock.unlink(missing_ok=True)
            except Exception:
                pass
