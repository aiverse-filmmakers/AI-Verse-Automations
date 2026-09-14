from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import COMPONENT_ID, __version__
from .config import default_state_dir, load_config, save_config, validate_target_config
from .engine import Engine
from .errors import AutomationsError
from .lifecycle import descriptor, doctor, set_enabled, setup, uninstall, update
from .os_extension import install_os_extension
from .service import serve
from .store import Store


def _json_arg(value: str):
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
    return json.loads(value)


def parser() -> argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog="ai-verse-automations"); p.add_argument("--state-dir",default=str(default_state_dir())); p.add_argument("--json",action="store_true")
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("install")
    s=sub.add_parser("setup"); s.add_argument("--os-root"); s.add_argument("--disabled",action="store_true")
    sub.add_parser("status"); sub.add_parser("doctor"); sub.add_parser("enable"); sub.add_parser("disable"); sub.add_parser("update"); sub.add_parser("uninstall")
    attach=sub.add_parser("attach-os"); attach.add_argument("--os-root")
    cfg=sub.add_parser("configure-target"); cfg.add_argument("kind",choices=["brain","bot","team_run","gateway"]); cfg.add_argument("config",type=_json_arg)
    c=sub.add_parser("create"); c.add_argument("--id"); c.add_argument("--name",required=True); c.add_argument("--scope",default="operator"); c.add_argument("--target-kind",required=True,choices=["brain","bot","team_run","gateway"]); c.add_argument("--target-ref"); c.add_argument("--action-class",default="read_local"); c.add_argument("--wake",required=True,type=_json_arg); c.add_argument("--retry",type=_json_arg)
    cd=sub.add_parser("create-definition"); cd.add_argument("--definition",required=True,type=_json_arg)
    t=sub.add_parser("add-trigger"); t.add_argument("automation_id"); t.add_argument("--id"); t.add_argument("--kind",required=True,choices=["once","interval","cron","webhook","event"]); t.add_argument("--spec",required=True,type=_json_arg)
    ew=sub.add_parser("edit-wake"); ew.add_argument("automation_id"); ew.add_argument("--wake",required=True,type=_json_arg)
    et=sub.add_parser("edit-trigger"); et.add_argument("trigger_id"); et.add_argument("--spec",required=True,type=_json_arg)
    for name in ("pause","resume","archive"):
        q=sub.add_parser(name); q.add_argument("automation_id")
    for name in ("pause-trigger","resume-trigger","archive-trigger"):
        q=sub.add_parser(name); q.add_argument("trigger_id")
    r=sub.add_parser("run-now"); r.add_argument("automation_id")
    l=sub.add_parser("list");
    runs=sub.add_parser("runs"); runs.add_argument("--automation-id"); runs.add_argument("--limit",type=int,default=100)
    sub.add_parser("tick"); sub.add_parser("recover");
    rr=sub.add_parser("retry-run"); rr.add_argument("run_id"); rr.add_argument("--owner-idempotency-confirmed",action="store_true")
    sub.add_parser("serve")
    e=sub.add_parser("emit-event"); e.add_argument("trigger_id"); e.add_argument("--event-id",required=True); e.add_argument("--source",required=True); e.add_argument("--event-type"); e.add_argument("--event",required=True,type=_json_arg)
    return p


def _out(value, as_json: bool):
    if as_json: print(json.dumps(value,indent=2,sort_keys=True,default=str))
    else:
        if isinstance(value,(dict,list)): print(json.dumps(value,indent=2,sort_keys=True,default=str))
        else: print(value)



def _normalize_global_args(argv):
    values=list(sys.argv[1:] if argv is None else argv)
    prefix=[]; rest=[]; i=0
    while i < len(values):
        if values[i]=="--json":
            prefix.append(values[i]); i+=1; continue
        if values[i]=="--state-dir":
            if i+1>=len(values): return values
            prefix.extend(values[i:i+2]); i+=2; continue
        rest.append(values[i]); i+=1
    return prefix+rest

def main(argv=None) -> int:
    args=parser().parse_args(_normalize_global_args(argv)); state_dir=Path(args.state_dir).expanduser().resolve(); store=Store(state_dir); engine=Engine(state_dir)
    try:
        cmd=args.command
        if cmd=="install": result={"component_id":COMPONENT_ID,"version":__version__,"state":"installed","next":"setup"}
        elif cmd=="setup": result=setup(state_dir,os_root=args.os_root,enable=not args.disabled)
        elif cmd=="status": result=descriptor(state_dir)
        elif cmd=="doctor": result=doctor(state_dir)
        elif cmd=="enable": result=set_enabled(state_dir,True)
        elif cmd=="disable": result=set_enabled(state_dir,False)
        elif cmd=="update": result=update(state_dir)
        elif cmd=="uninstall": result=uninstall(state_dir)
        elif cmd=="attach-os":
            cfg=load_config(state_dir,required=True)
            root=args.os_root or cfg.get("os_root")
            if not isinstance(root,str) or not root:
                raise AutomationsError("attach-os requires a configured or explicit OS root")
            result=install_os_extension(state_dir,Path(root).expanduser().resolve())
        elif cmd=="configure-target":
            cfg=load_config(state_dir,required=True); target_config=validate_target_config(args.kind,args.config); cfg.setdefault("targets",{})[args.kind]=target_config; save_config(state_dir,cfg); result={"ok":True,"kind":args.kind,"config":target_config}
        elif cmd=="create": result=store.create_automation(name=args.name,scope=args.scope,target_kind=args.target_kind,target_ref=args.target_ref,action_class=args.action_class,wake=args.wake,retry=args.retry,automation_id=args.id)
        elif cmd=="create-definition":
            d=args.definition
            if not isinstance(d,dict):
                raise AutomationsError("definition must be a JSON object")
            allowed={"name","scope","target_kind","target_ref","action_class","wake","retry","trigger","idempotency_key"}
            unknown=set(d)-allowed
            required={"name","scope","target_kind","action_class","wake","trigger","idempotency_key"}
            missing=required-set(d)
            if unknown or missing:
                raise AutomationsError("definition fields are invalid: "+", ".join(sorted(unknown|missing)))
            trigger=d.get("trigger")
            if not isinstance(trigger,dict) or set(trigger)!={"kind","spec"}:
                raise AutomationsError("definition trigger must contain exactly kind and spec")
            result=store.create_definition(
                name=d["name"],
                scope=d["scope"],
                target_kind=d["target_kind"],
                target_ref=d.get("target_ref"),
                action_class=d["action_class"],
                wake=d["wake"],
                retry=d.get("retry"),
                trigger_kind=trigger["kind"],
                trigger_spec=trigger["spec"],
                idempotency_key=d["idempotency_key"],
            )
        elif cmd=="add-trigger": result=store.create_trigger(automation_id=args.automation_id,kind=args.kind,spec=args.spec,trigger_id=args.id)
        elif cmd=="edit-wake": result=store.update_automation_wake(args.automation_id,args.wake)
        elif cmd=="edit-trigger": result=store.update_trigger(args.trigger_id,args.spec)
        elif cmd in {"pause","resume","archive"}: result=store.set_automation_state(args.automation_id,{"pause":"paused","resume":"active","archive":"archived"}[cmd])
        elif cmd in {"pause-trigger","resume-trigger","archive-trigger"}: result=store.set_trigger_state(args.trigger_id,{"pause-trigger":"paused","resume-trigger":"active","archive-trigger":"archived"}[cmd])
        elif cmd=="run-now": result=engine.run_now(args.automation_id)
        elif cmd=="list": result=store.list_automations()
        elif cmd=="runs": result=store.runs(args.automation_id,args.limit)
        elif cmd=="tick": result=engine.tick()
        elif cmd=="recover": result={"recovered":engine.recover_stale()}
        elif cmd=="retry-run": result=engine.retry_run(args.run_id,owner_idempotency_confirmed=args.owner_idempotency_confirmed)
        elif cmd=="emit-event": result=engine.ingest_event(trigger_id=args.trigger_id,event_id=args.event_id,event=args.event,source_kind="event",source=args.source,event_type=args.event_type)
        elif cmd=="serve": serve(state_dir); result={"ok":True}
        else: raise RuntimeError("unknown command")
        _out(result,args.json); return 0
    except AutomationsError as exc:
        _out({"ok":False,"code":getattr(exc,"code","AUTOMATIONS_ERROR"),"error":str(exc)},True); return 4
    except Exception as exc:
        _out({"ok":False,"code":"UNEXPECTED_ERROR","error":str(exc)},True); return 5

if __name__=="__main__": raise SystemExit(main())
