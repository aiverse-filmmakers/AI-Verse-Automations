from __future__ import annotations

from pathlib import Path

import pytest

from aiverse_automations.authority import os_permission
from aiverse_automations.errors import AuthorizationError


def test_real_subprocess_permission_binding(tmp_path: Path):
    root=tmp_path/"os"; scripts=root/"scripts"; scripts.mkdir(parents=True)
    script=scripts/"action-permission.mjs"
    script.write_text('''
let raw="";
for await (const chunk of process.stdin) raw += chunk;
const request=JSON.parse(raw);
process.stdout.write(JSON.stringify({...request,decision:"allow"})+"\\n");
''',encoding="utf-8")
    request={"action_class":"read_local","scope":"operator","request_fingerprint":"a"*64}
    result=os_permission(os_root=str(root),request=request)
    assert result["decision"]=="allow"


def test_permission_binding_mismatch_fails_closed(tmp_path: Path):
    root=tmp_path/"os"; scripts=root/"scripts"; scripts.mkdir(parents=True)
    script=scripts/"action-permission.mjs"
    script.write_text('process.stdout.write(JSON.stringify({request_fingerprint:"b".repeat(64),scope:"operator",action_class:"read_local",decision:"allow"})+"\\n");',encoding="utf-8")
    with pytest.raises(AuthorizationError):
        os_permission(os_root=str(root),request={"action_class":"read_local","scope":"operator","request_fingerprint":"a"*64})
