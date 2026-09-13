# Public-beta acceptance

Current local implementation evidence covers:

1. install/package build and CLI smoke;
2. setup bound to a real OS permission entrypoint;
3. machine-readable status/doctor lifecycle;
4. enable/disable/update/uninstall with state preservation;
5. one-time, interval and timezone cron schedules;
6. missed-run coalescing;
7. transactional concurrent due-claim de-duplication;
8. stable scheduled invocation identity;
9. event source/type binding;
10. persistent event replay de-duplication and drift rejection;
11. webhook HMAC and timestamp replay window;
12. local HTTP exposure restriction;
13. parent automation pause/resume without corrupting trigger lifecycle;
14. trigger pause/resume/edit lifecycle;
15. definition/version execution kill fences;
16. OS permission re-check on retry;
17. fail-closed permission response binding;
18. retry/backoff and dead-letter state;
19. owner-aware abandoned-claim recovery;
20. no timeout-only recovery of a live process owner;
21. explicit uncertain-delivery retry with original invocation ID;
22. current Brain idempotency adapter contract;
23. current Multiple Bots Phase 3.6 payload/projection compatibility;
24. raw credential rejection and secret-reference policy;
25. legacy OS automation definition detection as migration-required;
26. cross-platform GitHub Actions matrix for Linux/macOS/Windows, Python 3.11-3.13.

## Hosted evidence

Canonical repository: `aiverse-filmmakers/AI-Verse-Automations`.

Publication preserved the exact locally tested source tree before this evidence-only documentation update:

- local implementation commit: `f38f525769c9ad976e5dd016f8d577fa8a4e8035`;
- locally tested source tree: `0749fdb4259b1d7e28715eb8e367f1642d43db4e`;
- local test suite: **27/27 passed**;
- canonical GitHub publication commit containing that exact tree: `447310570aba837c1df61b9f87013f7cb7ec062b`;
- GitHub Actions CI run **34777167602**: **9/9 matrix jobs passed** across Ubuntu, macOS and Windows on Python 3.11, 3.12 and 3.13.

This document update records that completed hosted evidence. It does not claim an immutable tagged public release; release tagging/version pinning remains a separate distribution step.
