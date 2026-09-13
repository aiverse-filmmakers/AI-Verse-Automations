from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from .adapters import deliver
from .authority import os_permission
from .config import load_config
from .db import connect
from .errors import AuthorizationError, DeliveryError, StateConflict, ValidationError
from .schedule import advance_after_fire
from .util import bounded_json, canonical_json, iso, parse_time, sha256_json, utc_now

_PROCESS_BOOT_ID = uuid4().hex


def invocation_id(automation_id: str, trigger_id: str, occurrence: str) -> str:
    digest = hashlib.sha256(f"{automation_id}\0{trigger_id}\0{occurrence}".encode()).hexdigest()
    return f"inv_{digest[:40]}"


def retry_delay(policy: dict[str, Any], attempt: int) -> int:
    delay = policy["initial_seconds"] * (policy["backoff"] ** max(0, attempt - 1))
    return int(min(policy["max_seconds"], delay))


class Engine:
    def __init__(self, state_dir: Path, *, owner_id: str | None = None):
        self.state_dir = state_dir
        self.owner_id = owner_id or f"process:{os.getpid()}:{_PROCESS_BOOT_ID}"

    def _config(self) -> dict[str, Any]:
        return load_config(self.state_dir, required=True)

    def _claim_due(self, now: datetime) -> list[str]:
        conn=connect(self.state_dir); claimed=[]; now_iso=iso(now)
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows=conn.execute(
                """SELECT t.*,a.state AS automation_state,a.version AS automation_version,a.scope,a.target_kind,a.target_ref,a.action_class,a.wake_json,a.retry_json
                   FROM triggers t JOIN automations a ON a.id=t.automation_id
                   WHERE t.state='active' AND a.state='active' AND t.next_run_at IS NOT NULL AND t.next_run_at<=?
                   ORDER BY t.next_run_at,t.id""", (now_iso,)
            ).fetchall()
            for row in rows:
                spec=json.loads(row["spec_json"]); scheduled_for=row["next_run_at"]
                inv=invocation_id(row["automation_id"],row["id"],scheduled_for)
                wake=self._build_wake(dict(row),inv,now_iso,source_kind="schedule",scheduled_for=scheduled_for)
                fp=sha256_json({"automation_id":row["automation_id"],"trigger_id":row["id"],"invocation_id":inv,"scope":row["scope"],"action_class":row["action_class"],"wake":wake})
                run_id=f"run_{uuid4().hex}"
                try:
                    conn.execute(
                        """INSERT INTO runs(id,invocation_id,automation_id,automation_version,trigger_id,trigger_version,source_kind,scheduled_for,fired_at,status,attempt,claim_owner,request_fingerprint,wake_json,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (run_id,inv,row["automation_id"],row["automation_version"],row["id"],row["version"],"schedule",scheduled_for,now_iso,"claimed",0,self.owner_id,fp,canonical_json(wake),now_iso,now_iso)
                    )
                    claimed.append(run_id)
                except Exception as exc:
                    if "UNIQUE constraint failed: runs.invocation_id" not in str(exc): raise
                nxt=advance_after_fire(row["kind"],spec,scheduled_for=parse_time(scheduled_for),now=now)
                if nxt is None:
                    conn.execute("UPDATE triggers SET state='completed',next_run_at=NULL,updated_at=? WHERE id=?",(now_iso,row["id"]))
                else:
                    conn.execute("UPDATE triggers SET next_run_at=?,updated_at=? WHERE id=?",(nxt,now_iso,row["id"]))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK"); raise
        finally: conn.close()
        return claimed

    def _build_wake(self, row: dict[str, Any], inv: str, fired: str, *, source_kind: str, event: dict[str, Any] | None = None, event_source: str | None = None, event_type: str | None = None, scheduled_for: str | None = None) -> dict[str, Any]:
        payload=json.loads(row["wake_json"])
        base={
            "schema_version":"1.0",
            "automation_id":row["automation_id"],
            "trigger_id":row["id"],
            "invocation_id":inv,
            "scope":row["scope"],
            "fired_at":fired,
            "source_kind":source_kind,
            "target_kind":row["target_kind"],
            "target_ref":row["target_ref"],
            "payload":payload,
        }
        if scheduled_for is not None:
            base["scheduled_for"]=scheduled_for
        if row["target_kind"]=="brain":
            base["trigger_type"]=payload.get("trigger_type","scheduled_review" if source_kind=="schedule" else "event")
        if event is not None:
            base["event"] = event
            base["event_source"] = event_source
            if event_type is not None:
                base["event_type"] = event_type
        return base

    def _check_definition_fence(self, conn, run) -> tuple[dict[str,Any],dict[str,Any]]:
        a=conn.execute("SELECT * FROM automations WHERE id=?",(run["automation_id"],)).fetchone()
        t=conn.execute("SELECT * FROM triggers WHERE id=?",(run["trigger_id"],)).fetchone()
        if not a or not t: raise StateConflict("automation or trigger disappeared")
        if a["state"]!="active": raise StateConflict("automation is no longer active")
        allowed_trigger_states={"active","completed"}
        if run["source_kind"]=="manual": allowed_trigger_states.add("paused")
        if t["state"] not in allowed_trigger_states: raise StateConflict("trigger is no longer active for this invocation")
        if a["version"]!=run["automation_version"]: raise StateConflict("automation changed after run claim")
        if t["version"]!=run["trigger_version"]: raise StateConflict("trigger changed after run claim")
        return dict(a),dict(t)

    def execute_run(self, run_id: str) -> dict[str, Any]:
        config=self._config()
        if not config.get("enabled"):
            return self._terminal(run_id,"canceled","COMPONENT_DISABLED","Automations component is disabled")
        conn=connect(self.state_dir)
        try:
            conn.execute("BEGIN IMMEDIATE")
            run=conn.execute("SELECT * FROM runs WHERE id=?",(run_id,)).fetchone()
            if not run:
                conn.execute("ROLLBACK"); raise ValidationError(f"unknown run: {run_id}")
            run=dict(run)
            if run["status"] not in {"claimed","retry_wait"}:
                conn.execute("ROLLBACK"); return run
            try:
                automation, trigger=self._check_definition_fence(conn,run)
            except StateConflict as exc:
                now=utc_now(); conn.execute("UPDATE runs SET status='canceled',claim_owner=NULL,last_error_code='DEFINITION_REVOKED',last_error=?,updated_at=? WHERE id=?",(str(exc),now,run_id)); conn.execute("COMMIT"); return self.get_run(run_id)
            attempt=run["attempt"]+1; now=utc_now()
            conn.execute("UPDATE runs SET status='authorizing',attempt=?,claim_owner=?,next_attempt_at=NULL,updated_at=? WHERE id=?",(attempt,self.owner_id,now,run_id)); conn.execute("COMMIT")
        finally: conn.close()

        os_root=config.get("os_root")
        if not os_root:
            return self._terminal(run_id,"blocked","OS_NOT_CONFIGURED","AI-Verse OS root is required for wake authority")
        request={"action_class":automation["action_class"],"scope":automation["scope"],"request_fingerprint":run["request_fingerprint"]}
        try:
            decision=os_permission(os_root=os_root,request=request)
        except Exception as exc:
            return self._maybe_retry(run_id,automation,exc,code="OS_PERMISSION_UNAVAILABLE",retryable=True)
        if decision["decision"]=="approval_required":
            return self._terminal(run_id,"blocked","OS_APPROVAL_REQUIRED",decision.get("reason") or "approval required")
        if decision["decision"]!="allow":
            return self._terminal(run_id,"blocked","OS_DENIED",decision.get("reason") or "denied")

        wake=json.loads(run["wake_json"])
        target_cfg=(config.get("targets") or {}).get(automation["target_kind"])
        if not isinstance(target_cfg,dict):
            return self._terminal(run_id,"dead_letter","TARGET_NOT_CONFIGURED",f"target adapter not configured: {automation['target_kind']}")
        target_cfg=dict(target_cfg)
        if automation["target_kind"] in {"bot","team_run"}:
            target_cfg.setdefault("os_root", os_root)
        conn=connect(self.state_dir)
        try:
            conn.execute("UPDATE runs SET status='delivering',claim_owner=?,updated_at=? WHERE id=?",(self.owner_id,utc_now(),run_id))
        finally: conn.close()
        try:
            receipt=deliver(automation["target_kind"],target_cfg,wake,timeout=float(config.get("http_timeout_seconds",30)))
        except DeliveryError as exc:
            return self._maybe_retry(run_id,automation,exc,code=exc.code,retryable=exc.retryable)
        except Exception as exc:
            return self._maybe_retry(run_id,automation,exc,code="DELIVERY_EXCEPTION",retryable=False)
        return self._success(run_id,automation["target_kind"],receipt)

    def _maybe_retry(self, run_id: str, automation: dict[str,Any], exc: Exception, *, code: str, retryable: bool) -> dict[str,Any]:
        policy=json.loads(automation["retry_json"])
        run=self.get_run(run_id); attempt=int(run["attempt"])
        if retryable and attempt < policy["max_attempts"]:
            nxt=datetime.now(timezone.utc)+timedelta(seconds=retry_delay(policy,attempt))
            conn=connect(self.state_dir)
            try:
                conn.execute("UPDATE runs SET status='retry_wait',claim_owner=NULL,next_attempt_at=?,last_error_code=?,last_error=?,updated_at=? WHERE id=?",(iso(nxt),code,str(exc)[:2000],utc_now(),run_id))
            finally: conn.close()
            return self.get_run(run_id)
        return self._terminal(run_id,"dead_letter",code,str(exc))

    def _terminal(self,run_id:str,status:str,code:str,message:str)->dict[str,Any]:
        conn=connect(self.state_dir)
        try: conn.execute("UPDATE runs SET status=?,claim_owner=NULL,next_attempt_at=NULL,last_error_code=?,last_error=?,updated_at=? WHERE id=?",(status,code,message[:2000],utc_now(),run_id))
        finally: conn.close()
        return self.get_run(run_id)

    def _success(self,run_id:str,owner_kind:str,receipt:dict[str,Any])->dict[str,Any]:
        text=canonical_json(receipt)
        if len(text.encode())>65536: receipt={"status":receipt.get("status"),"truncated":True}
        digest=sha256_json(receipt); now=utc_now(); conn=connect(self.state_dir)
        try:
            conn.execute("BEGIN IMMEDIATE")
            run=conn.execute("SELECT invocation_id FROM runs WHERE id=?",(run_id,)).fetchone()
            if not run: raise ValidationError(f"unknown run: {run_id}")
            conn.execute("INSERT OR REPLACE INTO wake_receipts(invocation_id,owner_kind,owner_receipt_json,owner_receipt_digest,recorded_at) VALUES(?,?,?,?,?)",(run["invocation_id"],owner_kind,canonical_json(receipt),digest,now))
            conn.execute("UPDATE runs SET status='succeeded',claim_owner=NULL,receipt_json=?,next_attempt_at=NULL,last_error_code=NULL,last_error=NULL,updated_at=? WHERE id=?",(canonical_json({"owner_receipt_digest":digest,"owner":owner_kind}),now,run_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK"); raise
        finally: conn.close()
        return self.get_run(run_id)

    def get_run(self,run_id:str)->dict[str,Any]:
        conn=connect(self.state_dir)
        try:
            row=conn.execute("SELECT * FROM runs WHERE id=?",(run_id,)).fetchone()
            if not row: raise ValidationError(f"unknown run: {run_id}")
            x=dict(row); x["wake"]=json.loads(x.pop("wake_json")); x["receipt"]=json.loads(x.pop("receipt_json")) if x.get("receipt_json") else None; return x
        finally: conn.close()

    def tick(self, *, now: datetime | None = None) -> list[dict[str,Any]]:
        now=now or datetime.now(timezone.utc); config=self._config()
        if not config.get("enabled"): return []
        self.recover_stale(now=now)
        claimed=self._claim_due(now)
        conn=connect(self.state_dir)
        try:
            retry_rows=conn.execute("SELECT id FROM runs WHERE status='retry_wait' AND next_attempt_at<=? ORDER BY next_attempt_at,id",(iso(now),)).fetchall()
            retry_ids=[r["id"] for r in retry_rows]
        finally: conn.close()
        out=[]
        for run_id in claimed+retry_ids:
            out.append(self.execute_run(run_id))
        return out

    def recover_stale(self, *, now: datetime | None = None) -> list[str]:
        now=now or datetime.now(timezone.utc); config=self._config(); timeout=int(config.get("claim_timeout_seconds",300)); cutoff=iso(now-timedelta(seconds=timeout)); conn=connect(self.state_dir); recovered=[]
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows=conn.execute(
                """SELECT id FROM runs
                   WHERE status IN ('claimed','authorizing','delivering')
                     AND ((claim_owner IS NOT NULL AND claim_owner<>?) OR (claim_owner IS NULL AND updated_at<?))""",
                (self.owner_id,cutoff),
            ).fetchall()
            for row in rows:
                conn.execute("UPDATE runs SET status='unknown',claim_owner=NULL,last_error_code='CRASH_RECOVERY',last_error='previous scheduler owner ended before durable terminal receipt',updated_at=? WHERE id=?",(iso(now),row["id"])); recovered.append(row["id"])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK"); raise
        finally: conn.close()
        return recovered

    def retry_run(self, run_id: str, *, owner_idempotency_confirmed: bool = False) -> dict[str,Any]:
        run=self.get_run(run_id)
        if run["status"] not in {"unknown","dead_letter","blocked","failed"}:
            raise StateConflict("only unknown, dead-letter, blocked or failed runs can be recovered explicitly")
        if run["status"]=="unknown" and not owner_idempotency_confirmed:
            raise StateConflict("unknown delivery may already have caused an effect; retry requires explicit owner idempotency confirmation")
        conn=connect(self.state_dir)
        try:
            conn.execute("BEGIN IMMEDIATE")
            current=conn.execute("SELECT * FROM runs WHERE id=?",(run_id,)).fetchone()
            if not current:
                conn.execute("ROLLBACK"); raise ValidationError(f"unknown run: {run_id}")
            try:
                self._check_definition_fence(conn,dict(current))
            except StateConflict as exc:
                conn.execute("UPDATE runs SET status='canceled',claim_owner=NULL,last_error_code='DEFINITION_REVOKED',last_error=?,updated_at=? WHERE id=?",(str(exc),utc_now(),run_id))
                conn.execute("COMMIT")
                return self.get_run(run_id)
            conn.execute("UPDATE runs SET status='retry_wait',attempt=0,claim_owner=NULL,next_attempt_at=?,last_error_code='EXPLICIT_RECOVERY',last_error=NULL,updated_at=? WHERE id=?",(utc_now(),utc_now(),run_id))
            conn.execute("COMMIT")
        except Exception:
            try: conn.execute("ROLLBACK")
            except Exception: pass
            raise
        finally: conn.close()
        return self.execute_run(run_id)

    def run_now(self, automation_id: str) -> dict[str,Any]:
        conn=connect(self.state_dir); now=utc_now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            a=conn.execute("SELECT * FROM automations WHERE id=?",(automation_id,)).fetchone()
            if not a or a["state"]!="active": conn.execute("ROLLBACK"); raise StateConflict("automation must be active")
            t=conn.execute("SELECT * FROM triggers WHERE automation_id=? AND state IN ('active','completed','paused') ORDER BY created_at LIMIT 1",(automation_id,)).fetchone()
            if not t: conn.execute("ROLLBACK"); raise StateConflict("automation has no trigger")
            inv=f"inv_manual_{uuid4().hex}"; row={**dict(t),**{k:a[k] for k in a.keys()},"automation_id":automation_id,"id":t["id"]}
            wake=self._build_wake(row,inv,now,source_kind="manual")
            fp=sha256_json({"automation_id":automation_id,"trigger_id":t["id"],"invocation_id":inv,"scope":a["scope"],"action_class":a["action_class"],"wake":wake})
            run_id=f"run_{uuid4().hex}"
            conn.execute("INSERT INTO runs(id,invocation_id,automation_id,automation_version,trigger_id,trigger_version,source_kind,scheduled_for,fired_at,status,attempt,claim_owner,request_fingerprint,wake_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(run_id,inv,automation_id,a["version"],t["id"],t["version"],"manual",None,now,"claimed",0,self.owner_id,fp,canonical_json(wake),now,now)); conn.execute("COMMIT")
        except Exception:
            try: conn.execute("ROLLBACK")
            except Exception: pass
            raise
        finally: conn.close()
        return self.execute_run(run_id)

    def ingest_event(self, *, trigger_id: str, event_id: str, event: dict[str,Any], source_kind: str, source: str | None = None, event_type: str | None = None) -> dict[str,Any]:
        if not isinstance(event_id,str) or not event_id.strip() or len(event_id)>256 or "\x00" in event_id or "\n" in event_id or "\r" in event_id:
            raise ValidationError("event_id must be a bounded stable identifier")
        # Normalize and bound before hashing/persisting so event ingress cannot become an unbounded data store.
        if not isinstance(event,dict):
            raise ValidationError("event must be a JSON object")
        event=json.loads(bounded_json(event,max_bytes=65536,label="event"))
        now=utc_now(); digest=sha256_json({"source_kind":source_kind,"source":source,"event_type":event_type,"event":event}); conn=connect(self.state_dir)
        try:
            conn.execute("BEGIN IMMEDIATE")
            t=conn.execute("SELECT * FROM triggers WHERE id=?",(trigger_id,)).fetchone()
            if not t or t["state"]!="active": conn.execute("ROLLBACK"); raise StateConflict("trigger is not active")
            if t["kind"] not in {"webhook","event"}: conn.execute("ROLLBACK"); raise ValidationError("trigger does not accept events")
            spec=json.loads(t["spec_json"])
            if t["kind"]=="event":
                if source != spec.get("source"):
                    conn.execute("ROLLBACK"); raise ValidationError("event source does not match trigger authority")
                expected_type=spec.get("event_type")
                if expected_type is not None and event_type != expected_type:
                    conn.execute("ROLLBACK"); raise ValidationError("event type does not match trigger authority")
            elif t["kind"]=="webhook":
                expected_type=spec.get("event_type")
                if expected_type is not None and event_type != expected_type:
                    conn.execute("ROLLBACK"); raise ValidationError("webhook event type does not match trigger authority")
            a=conn.execute("SELECT * FROM automations WHERE id=?",(t["automation_id"],)).fetchone()
            if not a or a["state"]!="active": conn.execute("ROLLBACK"); raise StateConflict("automation is not active")
            prior=conn.execute("SELECT * FROM event_receipts WHERE trigger_id=? AND event_id=?",(trigger_id,event_id)).fetchone()
            if prior:
                conn.execute("ROLLBACK")
                if prior["event_digest"]!=digest: raise StateConflict("event replay changed payload")
                run_id=prior["run_id"]
                return self.get_run(run_id) if run_id else {"replayed":True}
            inv=invocation_id(a["id"],trigger_id,f"event:{event_id}")
            row={**dict(t),**{k:a[k] for k in a.keys()},"automation_id":a["id"],"id":trigger_id}
            wake=self._build_wake(row,inv,now,source_kind=source_kind,event=event,event_source=source,event_type=event_type)
            fp=sha256_json({"automation_id":a["id"],"trigger_id":trigger_id,"invocation_id":inv,"scope":a["scope"],"action_class":a["action_class"],"wake":wake})
            run_id=f"run_{uuid4().hex}"
            conn.execute("INSERT INTO runs(id,invocation_id,automation_id,automation_version,trigger_id,trigger_version,source_kind,scheduled_for,fired_at,status,attempt,claim_owner,request_fingerprint,wake_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(run_id,inv,a["id"],a["version"],trigger_id,t["version"],source_kind,None,now,"claimed",0,self.owner_id,fp,canonical_json(wake),now,now))
            conn.execute("INSERT INTO event_receipts(trigger_id,event_id,event_digest,received_at,run_id) VALUES(?,?,?,?,?)",(trigger_id,event_id,digest,now,run_id)); conn.execute("COMMIT")
        except Exception:
            try: conn.execute("ROLLBACK")
            except Exception: pass
            raise
        finally: conn.close()
        return self.execute_run(run_id)
