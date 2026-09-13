# AI-Verse Automations

Canonical scheduler, trigger and wake-delivery runtime for AI-Verse.

Automations owns **when** a bounded wake should occur and the durable invocation lifecycle. It never becomes the Brain, task coordinator, credential store, Memory, Skills registry or business database.

```text
schedule / event / webhook
  -> Automations durable occurrence claim
  -> current OS scope + permission evaluation
  -> Brain cognition OR Bot/Team Run OR Gateway owner boundary
  -> bounded owner acknowledgement
  -> Automations run/wake receipt
```

Public beta targets one local user and one local scheduler service first.

## 1. Install

```bash
python -m pip install .
ai-verse-automations install --json
```

Installation only makes the component executable. It grants no workspace authority, action permission, account access or downstream execution right.

## 2. Setup

```bash
ai-verse-automations setup --os-root /path/to/AI-Verse-OS --json
```

Setup:

- creates the component-owned SQLite scheduler store;
- binds the component to the current AI-Verse OS permission boundary;
- checks for pre-existing OS automation definitions before enabling;
- enables the local scheduler only when no migration handoff is required.

If old user-authored definitions are found under legacy OS cadence locations, status becomes `migration-required` and execution stays disabled. This prevents two competing writable automation authorities.

Setup does not move Brain ownership, copy Memory, grant Skills, create Bot Tasks or authorize external accounts.

Configure downstream owner adapters explicitly.

Brain:

```bash
ai-verse-automations configure-target brain \
  '{"root":"/path/to/AI-Verse-Brain","vendor":"hermes","host_adapter":"/path/to/brain-host.json"}' \
  --json
```

Current AI-Verse Multiple Bots:

```bash
ai-verse-automations configure-target bot \
  '{"url":"http://127.0.0.1:8787/v1/automations/invoke","protocol":"ai-verse-multiple-bots-v1"}' \
  --json
```

The Multiple Bots adapter generates bounded compatibility projections under the OS automation roots because the current Phase 3.6 receive-side contract still source-binds invocations there. Those files are explicitly projections, not canonical automation definitions.

Gateway:

```bash
ai-verse-automations configure-target gateway \
  '{"url":"http://127.0.0.1:8790/v1/automations/invoke"}' \
  --json
```

Gateway receives the stable AI-Verse wake envelope. A formal Gateway-specific adapter can replace this generic owner envelope without changing scheduler ownership.

Credentials are never accepted inline. Bearer credentials must use `bearer_token_ref` with `env:` or `file:`.

## 3. Verify

```bash
ai-verse-automations status --json
ai-verse-automations doctor --json
```

Possible top-level states include:

```text
setup-required
disabled
unhealthy
migration-required
ready
```

`doctor` reports structural, attachment/discovery, runtime, dependency and operational depth separately.

## 4. Use

Create an automation:

```bash
ai-verse-automations create \
  --name "Weekly Brain review" \
  --scope operator \
  --target-kind brain \
  --action-class read_local \
  --wake '{"trigger_type":"scheduled_review"}' \
  --json
```

Add a cron trigger:

```bash
ai-verse-automations add-trigger <automation-id> \
  --kind cron \
  --spec '{"expr":"0 9 * * MON","timezone":"Europe/Bucharest"}' \
  --json
```

Supported trigger classes:

- one-time timestamp;
- fixed interval;
- standard five-field cron with timezone;
- authenticated webhook;
- normalized local/event-source ingress.

Control the automation and trigger lifecycles:

```bash
ai-verse-automations pause <automation-id> --json
ai-verse-automations resume <automation-id> --json
ai-verse-automations pause-trigger <trigger-id> --json
ai-verse-automations resume-trigger <trigger-id> --json
ai-verse-automations edit-wake <automation-id> --wake @wake.json --json
ai-verse-automations edit-trigger <trigger-id> --spec @trigger.json --json
ai-verse-automations run-now <automation-id> --json
ai-verse-automations runs --automation-id <automation-id> --json
```

Run the long-lived local scheduler and webhook service:

```bash
ai-verse-automations serve
```

The public-beta HTTP server binds only to loopback. Remote webhook ingress should terminate TLS/authentication in a trusted reverse proxy or tunnel before forwarding locally.

### Webhooks

Create a webhook trigger with a secret reference:

```json
{
  "secret_ref": "env:MY_WEBHOOK_SECRET",
  "max_skew_seconds": 300,
  "event_type": "source.updated"
}
```

Send to `POST /v1/hooks/<trigger-id>` with:

```text
X-AI-Verse-Event-ID: stable-source-event-id
X-AI-Verse-Event-Type: source.updated
X-AI-Verse-Timestamp: unix-seconds
X-AI-Verse-Signature: sha256=<HMAC_SHA256(timestamp + "." + raw_body)>
```

The persistent `(trigger_id, event_id)` receipt prevents replay. Reusing an event ID with changed payload or metadata is rejected.

### Local events

```bash
ai-verse-automations emit-event <trigger-id> \
  --event-id source-event-123 \
  --source calendar \
  --event-type changed \
  --event '{"record_ref":"event:42"}' \
  --json
```

Both source and event type are bound to the trigger definition.

### Recovery

On process restart, in-flight claims owned by the previous process become `unknown`, not silently failed or automatically replayed. This covers the crash window where the downstream owner might have acted but Automations did not persist the acknowledgement.

Explicit recovery preserves the original invocation ID:

```bash
ai-verse-automations retry-run <run-id> --owner-idempotency-confirmed --json
```

For an `unknown` delivery the confirmation flag is mandatory. It asserts that the downstream owner honors the stable invocation ID idempotently. Dead-letter/blocked runs can also be explicitly retried after the underlying condition is repaired.

## 5. Update / disable / uninstall

```bash
ai-verse-automations disable --json
ai-verse-automations enable --json
ai-verse-automations update --json
ai-verse-automations uninstall --json
```

Uninstall/detach preserves the SQLite automation definitions and run history by default. Destructive purge is intentionally not part of `uninstall`.

## 6. What setup does and does not grant

Every delivery attempt re-checks the current OS action-permission boundary with the exact scope, action class and immutable request fingerprint. A workspace pause, policy change or other revocation therefore stops pending delivery or retry.

The scheduler has no remote mutation/control endpoint in public beta. Schedule creation and lifecycle mutation remain local operator actions. The loopback HTTP surface exposes authenticated webhook ingress and read-only projections only.

Automations does **not** own:

- strategic goals or strategy;
- Memory;
- Skills;
- Bot Tasks, Team Runs, Workers or execution attempts;
- external credentials;
- canonical business Data;
- Gateway sessions/runs after wake acceptance.

## Read-only local projections

For Dashboard and diagnostics, the local service exposes:

```text
GET /v1/status
GET /v1/automations
GET /v1/runs?automation_id=<id>&limit=100
```

These are projections over Automations-owned truth. No Dashboard-local automation database is required.
