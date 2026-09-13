from datetime import datetime, timezone
import hashlib, hmac

import pytest

from aiverse_automations.errors import ReplayError, ValidationError
from aiverse_automations.security import verify_webhook


def signature(secret: bytes,stamp:str,body:bytes)->str:
    return "sha256="+hmac.new(secret,stamp.encode()+b"."+body,hashlib.sha256).hexdigest()


def test_webhook_hmac_and_time_window(monkeypatch):
    monkeypatch.setenv("HOOK_SECRET","secret")
    now=datetime(2026,1,1,0,0,tzinfo=timezone.utc); stamp=str(int(now.timestamp())); body=b'{"ok":true}'
    verify_webhook(secret_ref="env:HOOK_SECRET",timestamp=stamp,signature=signature(b"secret",stamp,body),body=body,max_skew_seconds=300,now=now)
    with pytest.raises(ValidationError):
        verify_webhook(secret_ref="env:HOOK_SECRET",timestamp=stamp,signature=signature(b"wrong",stamp,body),body=body,max_skew_seconds=300,now=now)
    stale=str(int(now.timestamp())-301)
    with pytest.raises(ReplayError):
        verify_webhook(secret_ref="env:HOOK_SECRET",timestamp=stale,signature=signature(b"secret",stale,body),body=body,max_skew_seconds=300,now=now)
