# Security

AI-Verse Automations is a durable wake scheduler, not an authority grant.

## Permission and stale authority

Every delivery attempt re-checks the current AI-Verse OS action-permission boundary using an immutable request fingerprint, scope and action class. Cached allow decisions are never durable authority. Pending retries are re-authorized.

Automation/trigger version changes and parent pause/archive state are kill fences for already-claimed work.

## Schedule authority

Public beta exposes no mutable HTTP scheduler/control API. Creating, editing, pausing, resuming and running schedules are local CLI/operator actions. The read-only HTTP projection cannot create authority.

## Webhook authenticity and replay

Webhook triggers require:

- `env:` or absolute `file:` secret reference;
- HMAC-SHA256 over `timestamp + "." + raw_body`;
- bounded timestamp skew;
- bounded stable event ID;
- persistent replay receipt;
- optional exact event-type binding.

Inline webhook secrets are rejected by the trigger schema.

## External credentials

Target configuration whitelists exact fields. Raw bearer tokens and credential-bearing URLs are rejected. Bearer credentials must use `bearer_token_ref` and remain in environment/file credential ownership.

## Network exposure

The built-in HTTP server is loopback-only. Plain HTTP downstream owner endpoints are also loopback-only. Remote owner endpoints require HTTPS. Remote webhook exposure requires a separate trusted TLS/authenticated reverse proxy or tunnel.

## Path safety

Generated Multiple Bots compatibility projections:

- remain inside the configured real OS root;
- reject symlinked path components;
- use exclusive file creation;
- contain only bounded IDs/digests, never the source prompt/body.

Setup refuses a symlink OS root.

## Bounded storage and delivery

Wake/event bodies and owner receipts are bounded. Automations stores scheduler state and bounded run evidence only. It must not become a credential store, general Memory system, Skills database, Bot task database or canonical business Data store.

## Crash uncertainty

A process crash between downstream effect and durable acknowledgement produces `unknown`, not automatic replay. Recovery requires explicit idempotency confirmation and reuses the same invocation ID.
