from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import load_config
from .engine import Engine
from .errors import AutomationsError, ValidationError
from .lifecycle import descriptor
from .security import verify_webhook
from .store import Store

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class WebhookServer:
    def __init__(self, state_dir: Path, host: str, port: int):
        if host not in _LOOPBACK:
            raise ValidationError("public-beta HTTP control/webhook server is loopback-only; use a TLS-authenticated reverse proxy for remote ingress")
        self.state_dir=state_dir; self.engine=Engine(state_dir); self.store=Store(state_dir)
        outer=self
        class Handler(BaseHTTPRequestHandler):
            server_version="AI-Verse-Automations/0.1"
            def _reply(self,status:int,body):
                raw=json.dumps(body,sort_keys=True,default=str).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(raw))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(raw)
            def do_GET(self):
                parsed=urlparse(self.path); path=parsed.path
                if path=="/v1/status": return self._reply(200,descriptor(outer.state_dir))
                if path=="/v1/automations": return self._reply(200,{"items":outer.store.list_automations()})
                if path=="/v1/runs":
                    query=parse_qs(parsed.query); automation_id=(query.get("automation_id") or [None])[0]
                    try: limit=min(500,max(1,int((query.get("limit") or [100])[0])))
                    except Exception: return self._reply(400,{"ok":False,"error":"invalid limit"})
                    return self._reply(200,{"items":outer.store.runs(automation_id,limit)})
                return self._reply(404,{"ok":False,"error":"not found"})
            def do_POST(self):
                path=urlparse(self.path).path; parts=[p for p in path.split("/") if p]
                if len(parts)!=3 or parts[:2]!=["v1","hooks"]: return self._reply(404,{"ok":False,"error":"not found"})
                trigger_id=parts[2]
                config=load_config(outer.state_dir,required=True); max_body=int(config.get("webhook",{}).get("max_body_bytes",65536))
                try:
                    length=int(self.headers.get("Content-Length","0"))
                    if length<0 or length>max_body: return self._reply(413,{"ok":False,"error":"body too large"})
                    body=self.rfile.read(length)
                    trigger=outer.store.get_trigger(trigger_id)
                    if trigger["kind"]!="webhook": return self._reply(404,{"ok":False,"error":"not a webhook trigger"})
                    event_id=self.headers.get("X-AI-Verse-Event-ID","").strip()
                    timestamp=self.headers.get("X-AI-Verse-Timestamp","").strip()
                    signature=self.headers.get("X-AI-Verse-Signature","").strip()
                    if not event_id or len(event_id)>256 or "\x00" in event_id or "\n" in event_id or "\r" in event_id: return self._reply(400,{"ok":False,"error":"bounded event id required"})
                    verify_webhook(secret_ref=trigger["spec"]["secret_ref"],timestamp=timestamp,signature=signature,body=body,max_skew_seconds=int(trigger["spec"].get("max_skew_seconds",300)))
                    event=json.loads(body.decode("utf-8")) if body else {}
                    event_type=self.headers.get("X-AI-Verse-Event-Type")
                    result=outer.engine.ingest_event(trigger_id=trigger_id,event_id=event_id,event=event,source_kind="webhook",source="webhook",event_type=event_type)
                    self._reply(202,{"ok":True,"run_id":result.get("id"),"status":result.get("status")})
                except AutomationsError as exc:
                    self._reply(409,{"ok":False,"code":getattr(exc,"code","AUTOMATIONS_ERROR"),"error":str(exc)})
                except Exception as exc:
                    self._reply(400,{"ok":False,"error":str(exc)})
            def log_message(self,format,*args):
                return
        self.httpd=ThreadingHTTPServer((host,port),Handler)
    @property
    def address(self): return self.httpd.server_address
    def serve_forever(self): self.httpd.serve_forever(poll_interval=0.5)
    def shutdown(self): self.httpd.shutdown(); self.httpd.server_close()
