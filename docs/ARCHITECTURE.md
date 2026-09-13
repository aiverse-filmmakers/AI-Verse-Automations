# Architecture

## Canonical ownership

AI-Verse Automations owns schedule/trigger definitions, occurrence identity, trigger state, retry/dead-letter state and wake-delivery receipts.

```text
schedule / normalized event / verified webhook
  -> durable trigger occurrence
  -> immutable invocation identity
  -> definition/version fence
  -> current AI-Verse OS permission floor
  -> downstream owner boundary
  -> bounded receipt
```

The downstream owner owns the work created by the wake.

- Brain owns goals, strategy, cognition and evaluation.
- Multiple Bots owns Bots, Tasks, Team Runs, Workers, leases, approvals and coordination recovery.
- Gateway owns client/session/run ingress and host execution-loop state.
- Memory owns historical memory.
- Skills owns reusable capability packages.
- Data owns canonical structured operational records.

## Durable store

SQLite is the component-owned canonical scheduler store and uses WAL plus transactional claims.

It stores:

- automation definitions and version fences;
- triggers and next scheduled occurrence;
- persistent event replay receipts;
- run attempts and retry/dead-letter state;
- bounded wake acknowledgement digests;
- process claim owner for restart recovery.

It is not a general event lake or business database. Wake/event bodies are bounded to 64 KiB.

## Occurrence and idempotency identity

Scheduled occurrence:

```text
sha256(automation_id + trigger_id + exact_scheduled_instant)
```

Event/webhook occurrence:

```text
sha256(automation_id + trigger_id + stable_source_event_id)
```

Event replay also binds the normalized source, event type and body digest. A repeated stable event ID may return the existing run only when those semantics are unchanged.

## Scheduling behavior

Supported time triggers:

- one-time UTC/offset timestamp;
- interval from 60 seconds to one year;
- five-field cron with IANA timezone.

Cron evaluation scans UTC instants and tests their local representation. This avoids generating nonexistent DST local times and distinguishes repeated fall-back instants.

Recurring misfires coalesce. After an outage, one overdue occurrence is claimed and the trigger advances to its next future instant rather than replaying an unbounded backlog.

The claim and schedule advance happen in one `BEGIN IMMEDIATE` transaction. Concurrent scheduler ticks therefore converge on one occurrence/run.

## Restart and uncertain delivery

Each live claim records a process owner identity. A restarted process can immediately distinguish claims owned by the previous process from its own live work. It does not depend only on an age/TTL heuristic.

A process-owned in-flight run that loses its owner becomes:

```text
unknown
```

This state means the downstream effect may or may not have happened. It is never automatically replayed.

Explicit retry uses the same immutable invocation ID and requires an operator confirmation for `unknown` runs that the downstream owner provides idempotent handling.

Legacy rows with no process owner use an age cutoff as a compatibility fallback.

## Authority and revocation

Stored scope/action class are a request, not permission.

Before every delivery attempt Automations invokes the OS `action-permission.mjs` boundary using:

- exact action class;
- exact operator/workspace scope;
- immutable request fingerprint.

The response must bind back to those exact values. Unknown, malformed or unavailable permission responses fail closed.

`approval_required` is not treated as approval. Automations stops the delivery and records `blocked` until an explicit supported approval flow resolves the condition outside the scheduler.

Automation and trigger versions are execution fences. Operator mutation after an occurrence claim cancels the stale claim before delivery.

## Multiple Bots compatibility bridge

Current AI-Verse Multiple Bots Phase 3.6 revalidates a source file under AI-Verse OS `automations/jobs`, `automations/triggers` or workspace automations before accepting an invocation.

Automations preserves the current receive-side contract without surrendering canonical ownership by generating an invocation-specific compatibility projection:

```text
AI-Verse OS/automations/jobs/.ai-verse-automations/<invocation-id>.json
AI-Verse OS/automations/triggers/.ai-verse-automations/<invocation-id>.json
```

The projection contains only IDs/digests and an explicit `projection_only` marker. It does not contain the recurring prompt/objective or credentials. Multiple Bots receives the exact current camelCase Phase 3.6 payload and remains the owner of resulting coordination state.

## HTTP surface

Public beta binds only to loopback.

Mutable schedule control remains CLI/operator-local. The HTTP surface provides:

- HMAC-authenticated webhook ingress;
- read-only status/automation/run projections.

Remote exposure requires an external TLS-authenticated reverse proxy/tunnel. This avoids pretending the built-in development HTTP server is an Internet-grade auth/TLS boundary.
