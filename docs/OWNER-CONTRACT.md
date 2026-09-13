# Owner wake contract

## Common envelope

The internal stable wake envelope includes:

- `schema_version`
- `automation_id`
- `trigger_id`
- `invocation_id`
- exact `scope`
- `fired_at`
- optional `scheduled_for`
- `source_kind`
- `target_kind`
- optional `target_ref`
- bounded owner-specific `payload`
- normalized event metadata for event/webhook wakes

`invocation_id` is the downstream idempotency key. A successful downstream response is an acknowledgement, not transfer of the downstream canonical domain into Automations.

## Brain

The built-in adapter invokes current Brain:

```text
ai-verse-brain run-tick ...
  --scope <scope>
  --trigger <type>
  --idempotency-key <invocation_id>
```

Brain remains the owner of intent, goals, strategy, cognition and verification. Automations never evaluates whether a goal is complete or chooses strategy.

## Multiple Bots

For current Multiple Bots Phase 3.6 the adapter translates the stable wake into the exact receive-side contract:

```text
POST /v1/automations/invoke
```

It supplies:

- automation ID;
- invocation ID;
- exact workspace ID;
- fired timestamp;
- source kind/path/digest compatibility projection;
- Bot or Team Run target;
- bounded objective/request fields.

Multiple Bots remains the owner of Tasks, Team Runs, Workers, capability leases, approvals, execution attempts, cancellation and coordination evidence.

## Gateway

Gateway targets receive the common stable wake envelope through a configured owner endpoint. Gateway owns sessions/runs and execution-loop state after acknowledgement.

Until the canonical AI-Verse-Gateway repository publishes a versioned receive-side schema, Automations deliberately does not invent Gateway session internals. The adapter boundary is replaceable without changing scheduler state or ownership.

## Dashboard

Current Dashboard protocol names future automation views/commands as:

- `cron.list`
- `cron.history`
- `cron.create`
- `cron.pause`
- `cron.resume`
- `cron.runNow`

Current Dashboard is still read-only for command methods. Automations therefore exposes read-only local projections now and does not add an unauthenticated mutable HTTP schedule API merely to satisfy future UI names.
