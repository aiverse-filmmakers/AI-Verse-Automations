from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
from typing import Any
from urllib.parse import urlparse

from .errors import DeliveryError, ValidationError
from .security import resolve_secret
from .util import bounded_json, canonical_json

MAX_WAKE_BYTES = 65536
MAX_RECEIPT_BYTES = 65536


def _safe_http_target(url: str) -> tuple[str, str, int, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValidationError("owner endpoint must be http(s)")
    if parsed.username or parsed.password:
        raise ValidationError("owner endpoint credentials must not be embedded in the URL")
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValidationError("plain HTTP owner endpoints must be loopback-only")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return parsed.scheme, host, port, path


def _http_post(url: str, body: dict[str, Any], *, timeout: float, bearer_token_ref: str | None = None) -> dict[str, Any]:
    scheme, host, port, path = _safe_http_target(url)
    raw = bounded_json(body, max_bytes=MAX_WAKE_BYTES, label="wake").encode("utf-8")
    conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(host, port, timeout=timeout)
    headers = {"Content-Type": "application/json", "Content-Length": str(len(raw)), "User-Agent": "AI-Verse-Automations/0.1"}
    if bearer_token_ref:
        headers["Authorization"] = "Bearer " + resolve_secret(bearer_token_ref).decode("utf-8")
    try:
        conn.request("POST", path, body=raw, headers=headers)
        response = conn.getresponse()
        payload = response.read(MAX_RECEIPT_BYTES + 1)
    except (OSError, socket.timeout) as exc:
        raise DeliveryError(f"owner endpoint unavailable: {exc}", retryable=True, code="OWNER_UNAVAILABLE") from exc
    finally:
        conn.close()
    if len(payload) > MAX_RECEIPT_BYTES:
        raise DeliveryError("owner receipt exceeds safety bound", retryable=False, code="RECEIPT_TOO_LARGE")
    text = payload.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text else {}
    except Exception:
        parsed = {"text": text}
    receipt = {"status": response.status, "body": parsed}
    if 200 <= response.status < 300:
        return receipt
    # 409 is intentionally retryable because owner APIs may use it for transient concurrency.
    retryable = response.status in {408, 409, 425, 429} or 500 <= response.status < 600
    raise DeliveryError(f"owner endpoint returned HTTP {response.status}", retryable=retryable, code=f"OWNER_HTTP_{response.status}")


def deliver_brain(config: dict[str, Any], wake: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    root = config.get("root")
    vendor = config.get("vendor")
    if not root or vendor not in {"claude", "codex", "hermes"}:
        raise ValidationError("brain target requires root and vendor")
    command = [config.get("binary") or "ai-verse-brain", "run-tick", str(Path(root).expanduser().resolve()), "--vendor", vendor]
    host_adapter = config.get("host_adapter")
    if host_adapter:
        command += ["--host-adapter", str(Path(host_adapter).expanduser().resolve())]
    elif config.get("read_only_context") is True:
        command += ["--read-only-context"]
    else:
        raise ValidationError("brain target requires host_adapter or read_only_context=true")
    command += ["--scope", wake["scope"], "--trigger", wake.get("trigger_type", "event"), "--idempotency-key", wake["invocation_id"]]
    if config.get("model"):
        command += ["--model", config["model"]]
    if config.get("provider"):
        command += ["--provider", config["provider"]]
    try:
        proc = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeliveryError(f"Brain invocation failed to start/complete: {exc}", retryable=True, code="BRAIN_UNAVAILABLE") from exc
    stdout = proc.stdout[-MAX_RECEIPT_BYTES:]
    if proc.returncode == 0:
        try:
            body = json.loads(stdout) if stdout.strip() else {}
        except Exception:
            body = {"text": stdout}
        return {"status": 200, "body": body}
    retryable = proc.returncode in {75, 111}
    raise DeliveryError(f"Brain rejected wake: {proc.stderr.strip() or stdout or proc.returncode}", retryable=retryable, code="BRAIN_REJECTED")


def _assert_projection_parent(os_root: Path, parent: Path) -> None:
    root_real = os_root.resolve(strict=True)
    try:
        parent.relative_to(os_root)
    except ValueError as exc:
        raise ValidationError("compatibility projection escaped the OS root") from exc
    cursor = os_root
    for part in parent.relative_to(os_root).parts:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise ValidationError(f"compatibility projection path contains symlink: {cursor}")
    parent.mkdir(parents=True, exist_ok=True)
    parent_real = parent.resolve(strict=True)
    if parent_real != root_real and root_real not in parent_real.parents:
        raise ValidationError("compatibility projection resolved outside the OS root")


def _multiple_bots_projection(config: dict[str, Any], wake: dict[str, Any]) -> tuple[str, str, str]:
    os_root_raw = config.get("os_root")
    if not isinstance(os_root_raw, str) or not os_root_raw:
        raise ValidationError("Multiple Bots compatibility adapter requires os_root")
    requested_root = Path(os_root_raw).expanduser()
    if requested_root.is_symlink():
        raise ValidationError("Multiple Bots compatibility adapter refuses a symlink OS root")
    os_root = requested_root.resolve()
    if not os_root.is_dir():
        raise ValidationError("Multiple Bots compatibility adapter requires a regular OS root directory")
    source_kind = "job" if wake["source_kind"] in {"schedule", "manual"} else "trigger"
    rel = Path("automations") / ("jobs" if source_kind == "job" else "triggers") / ".ai-verse-automations" / f"{wake['invocation_id']}.json"
    physical = os_root / rel
    _assert_projection_parent(os_root, physical.parent)
    projection = {
        "schema_version": "1.0",
        "projection_only": True,
        "owner": "AI-Verse-Automations",
        "automation_id": wake["automation_id"],
        "trigger_id": wake["trigger_id"],
        "invocation_id": wake["invocation_id"],
        "request_digest": hashlib.sha256(canonical_json(wake).encode("utf-8")).hexdigest(),
    }
    raw = (canonical_json(projection) + "\n").encode("utf-8")
    if physical.exists():
        if physical.is_symlink() or not physical.is_file():
            raise ValidationError("existing Multiple Bots projection is unsafe")
        existing = physical.read_bytes()
        if existing != raw:
            raise ValidationError("existing Multiple Bots projection conflicts with this invocation")
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(str(physical), flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    digest = hashlib.sha256(raw).hexdigest()
    return source_kind, rel.as_posix(), digest


def _multiple_bots_body(config: dict[str, Any], wake: dict[str, Any]) -> dict[str, Any]:
    if not str(wake.get("scope", "")).startswith("workspace:"):
        raise ValidationError("Multiple Bots automation wakes require workspace:<id> scope")
    workspace_id = wake["scope"].split(":", 1)[1]
    if not workspace_id:
        raise ValidationError("Multiple Bots workspace scope is empty")
    target_ref = wake.get("target_ref")
    if not isinstance(target_ref, str) or not target_ref:
        raise ValidationError("Multiple Bots target_ref is required")
    payload = wake.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValidationError("wake payload must be an object")
    objective = payload.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise ValidationError("Multiple Bots wake payload requires objective")
    source_kind, source_path, source_digest = _multiple_bots_projection(config, wake)
    if wake["target_kind"] == "bot":
        target: dict[str, Any] = {"kind": "bot", "botId": target_ref}
    else:
        target = {"kind": "team_run", "leaderId": target_ref}
        if payload.get("topology") is not None:
            target["topology"] = payload["topology"]
    body: dict[str, Any] = {
        "automationId": wake["automation_id"],
        "invocationId": wake["invocation_id"],
        "workspaceId": workspace_id,
        "firedAt": wake["fired_at"],
        "source": {"kind": source_kind, "path": source_path, "digest": source_digest},
        "target": target,
        "objective": objective.strip(),
    }
    # Exact current receive-side names from AI-Verse-Multiple-Bots Phase 3.6.
    optional = {
        "reason": "reason",
        "requiredConstraints": "requiredConstraints",
        "expectedOutput": "expectedOutput",
        "memoryRecall": "memoryRecall",
        "skillRefs": "skillRefs",
        "tools": "tools",
        "connections": "connections",
        "budget": "budget",
        "maxHops": "maxHops",
        "deadlineAt": "deadlineAt",
        "leaseExpiresAt": "leaseExpiresAt",
        "approval": "approval",
        "recoveryPolicy": "recoveryPolicy",
        "maxAttempts": "maxAttempts",
    }
    for source_name, target_name in optional.items():
        if source_name in payload:
            body[target_name] = payload[source_name]
    return body


def deliver(target_kind: str, target_config: dict[str, Any], wake: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    if target_kind == "brain":
        return deliver_brain(target_config, wake, timeout=timeout)
    if target_kind in {"bot", "team_run"}:
        url = target_config.get("url")
        if not isinstance(url, str):
            raise ValidationError(f"{target_kind} target requires url")
        protocol = target_config.get("protocol", "ai-verse-multiple-bots-v1")
        if protocol != "ai-verse-multiple-bots-v1":
            raise ValidationError("unsupported Multiple Bots automation protocol")
        body = _multiple_bots_body(target_config, wake)
        return _http_post(url, body, timeout=timeout, bearer_token_ref=target_config.get("bearer_token_ref"))
    if target_kind == "gateway":
        url = target_config.get("url")
        if not isinstance(url, str):
            raise ValidationError("gateway target requires url")
        return _http_post(url, wake, timeout=timeout, bearer_token_ref=target_config.get("bearer_token_ref"))
    raise ValidationError(f"unsupported target kind: {target_kind}")
