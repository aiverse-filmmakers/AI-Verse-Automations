from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

from .errors import AuthorizationError, ValidationError


def os_permission(*, os_root: str, request: dict[str, Any], timeout_seconds: float = 10.0) -> dict[str, Any]:
    root = Path(os_root).expanduser().resolve()
    script = root / "scripts" / "action-permission.mjs"
    if script.is_symlink() or not script.is_file():
        raise AuthorizationError("AI-Verse OS action permission boundary is unavailable")
    proc = subprocess.run(
        ["node", str(script), "--root", str(root)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if proc.returncode != 0:
        raise AuthorizationError(f"OS permission check failed closed: {proc.stderr.strip() or proc.returncode}")
    try:
        result = json.loads(proc.stdout)
    except Exception as exc:
        raise AuthorizationError("OS permission check returned invalid JSON") from exc
    for key in ("request_fingerprint", "scope", "action_class", "decision"):
        if key not in result:
            raise AuthorizationError(f"OS permission response missing {key}")
    if result["request_fingerprint"] != request["request_fingerprint"] or result["scope"] != request["scope"] or result["action_class"] != request["action_class"]:
        raise AuthorizationError("OS permission response binding mismatch")
    if result["decision"] not in {"allow", "approval_required", "deny"}:
        raise AuthorizationError("OS permission response has unknown decision")
    return result


def require_allowed(result: dict[str, Any]) -> None:
    if result["decision"] == "allow":
        return
    if result["decision"] == "approval_required":
        raise AuthorizationError("OS approval is required for this exact wake")
    raise AuthorizationError("OS denied this exact wake")
