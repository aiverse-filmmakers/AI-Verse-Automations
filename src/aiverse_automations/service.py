from __future__ import annotations

import signal
import threading
import time
from pathlib import Path

from .config import load_config
from .engine import Engine
from .webhook import WebhookServer


def serve(state_dir: Path) -> None:
    config=load_config(state_dir,required=True)
    if not config.get("enabled"):
        raise RuntimeError("Automations component is disabled")
    engine=Engine(state_dir); engine.recover_stale()
    wh=config.get("webhook",{}); server=WebhookServer(state_dir,wh.get("host","127.0.0.1"),int(wh.get("port",8766)))
    thread=threading.Thread(target=server.serve_forever,name="aiverse-webhooks",daemon=True); thread.start()
    stop=False
    def _stop(*_):
        nonlocal stop; stop=True
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT,signal.SIGTERM): signal.signal(sig,_stop)
    tick=max(1,int(config.get("tick_seconds",15)))
    try:
        while not stop:
            engine.tick(); time.sleep(tick)
    finally:
        server.shutdown(); thread.join(timeout=3)
