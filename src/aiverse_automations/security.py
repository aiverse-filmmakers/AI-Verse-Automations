from __future__ import annotations

from datetime import datetime, timezone
import hmac
import hashlib
import os
from pathlib import Path

from .errors import ReplayError, ValidationError


def resolve_secret(secret_ref: str) -> bytes:
    if secret_ref.startswith("env:"):
        name = secret_ref[4:]
        if not name or not name.replace("_", "").isalnum():
            raise ValidationError("invalid env secret reference")
        value = os.getenv(name)
        if not value:
            raise ValidationError(f"secret environment variable is unavailable: {name}")
        return value.encode("utf-8")
    if secret_ref.startswith("file:"):
        path = Path(secret_ref[5:]).expanduser()
        if not path.is_absolute():
            raise ValidationError("secret file reference must be absolute")
        if path.is_symlink() or not path.is_file():
            raise ValidationError("secret file must be a regular non-symlink file")
        if path.stat().st_size > 65536:
            raise ValidationError("secret file is too large")
        value = path.read_bytes().strip()
        if not value:
            raise ValidationError("secret file is empty")
        return value
    raise ValidationError("unsupported secret reference")


def verify_webhook(*, secret_ref: str, timestamp: str, signature: str, body: bytes, max_skew_seconds: int = 300, now: datetime | None = None) -> None:
    try:
        stamp = int(timestamp)
    except Exception as exc:
        raise ReplayError("invalid webhook timestamp") from exc
    now = now or datetime.now(timezone.utc)
    if abs(int(now.timestamp()) - stamp) > max_skew_seconds:
        raise ReplayError("webhook timestamp is outside the allowed replay window")
    if not signature.startswith("sha256="):
        raise ValidationError("webhook signature must use sha256=<hex>")
    provided = signature[len("sha256="):]
    if len(provided) != 64:
        raise ValidationError("webhook signature has invalid length")
    secret = resolve_secret(secret_ref)
    message = timestamp.encode("ascii") + b"." + body
    expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise ValidationError("webhook signature verification failed")
