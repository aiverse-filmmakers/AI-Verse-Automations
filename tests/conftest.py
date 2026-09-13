from __future__ import annotations

import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aiverse_automations.lifecycle import setup
from aiverse_automations.config import load_config, save_config


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    root=tmp_path/"state"
    os_root=tmp_path/"os"; (os_root/"scripts").mkdir(parents=True); (os_root/"scripts"/"action-permission.mjs").write_text("// test boundary\n",encoding="utf-8")
    setup(root,os_root=str(os_root),enable=True)
    return root


@pytest.fixture
def allow_os(monkeypatch):
    def allow(*, os_root, request, timeout_seconds=10.0):
        return {**request,"decision":"allow","source":"test"}
    monkeypatch.setattr("aiverse_automations.engine.os_permission",allow)
    return allow


@pytest.fixture
def owner_server():
    received=[]
    behavior={"status":200,"body":{"ok":True}}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n=int(self.headers.get("Content-Length","0")); raw=self.rfile.read(n); received.append(json.loads(raw))
            body=json.dumps(behavior["body"]).encode(); self.send_response(behavior["status"]); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/invoke",received,behavior
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
