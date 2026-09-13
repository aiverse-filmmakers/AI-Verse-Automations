# Benchmark evidence

Public-beta design was benchmarked before implementation against mature agent schedulers and durable job systems.

## Hermes cron / heartbeat

Adopted:

- scheduler-owned pause/resume/run-now lifecycle;
- durable attempt state before executor dispatch;
- claimed/running/terminal distinction;
- occurrence de-duplication;
- crash recovery that does not pretend an uncertain side effect definitely failed;
- fresh bounded execution entrypoints.

A key failure lesson is that TTL-only claim recovery is not enough. AI-Verse therefore records process ownership and treats abandoned delivery as `unknown` while refusing automatic uncertain replay.

## OpenClaw schedules / hooks

Adopted:

- deterministic schedule bookkeeping outside model cognition;
- schedule mechanics separated from the payload that wakes an agent/runtime;
- persisted jobs that survive process restart;
- scheduler wake into an owner runtime rather than scheduler-owned reasoning.

AI-Verse strengthens this with an OS permission re-check on every actual delivery attempt.

## Letta scheduled continuation

Adopted:

- explicit future invocation distinct from the current agent turn;
- time schedules separate from event/reactive trigger classes;
- durable host wake instead of implicit model self-continuation.

## Temporal and durable execution systems

Adopted:

- durable claim before dispatch;
- explicit retry classification;
- stable idempotency identity around side effects;
- uncertainty after crash in the effect-to-acknowledgement window;
- recovery semantics that do not blindly repeat an arbitrary effect.

## What AI-Verse deliberately does not copy

- scheduler state inside Brain/model memory;
- periodic blind cognition with no resource/authority boundary;
- raw cron shell execution as the canonical agent contract;
- infinite missed-run backlog replay;
- cached permission grants;
- unauthenticated remote mutation API;
- inline webhook/owner credentials;
- schedule creation that itself grants downstream action authority.
