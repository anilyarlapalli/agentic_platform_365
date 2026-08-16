# Open work

Status as of 2026-08-16. Items 1 and 2 (the GraphRAG flag and its engine-root
default) are closed in Phase 17 — see ROLLOUT.md.

`ROLLOUT.md` records what happened and why. This file records what has **not**
happened yet, and the order to do it in. Update both.

Every item is classified by *why* it is outstanding, because these are three
different kinds of job:

| Class | Meaning |
| --- | --- |
| **Defect** | Code that is wrong right now. |
| **Unproven** | It exists, but nothing demonstrates it works. |
| **Absent** | The capability was never built. |
| **Action** | A task for a person, not for the codebase. |

Current tally: **3 defects, 11 unproven, 10 absent, 3 actions.**

The shape of this list is the honest summary of the project: very little is
broken, a great deal is unverified.

---

## Defects

### 1. `GRAPHRAG_ENABLED` gates nothing

The flag appears only in its `settings.py` declaration and one
`check_coherence` existence branch. `POST /api/query` with `mode:"graph"` calls
`engine.install()` unconditionally.

Its effect is **inverted**: setting it `false` — as
`deploy/k8s/base/foundation.yaml` does — removes the startup check, so graph
mode fails at request time instead of at boot. `README.md` states the opposite.

*~1h — fix, property test, README correction.*

### 2. Engine root defaults to a personal absolute path

`graphrag_engine_root` defaults to `/home/anil-y/app_ideas/…`, and that default
is live in the production manifests. In a container it resolves to nothing.

*~30m — folds into item 1.*

### 3. The API inherits a 35-minute stop grace period

`stop_grace_period: 35m` sits on the shared YAML anchor in
`deploy/compose.runtime.yml`, so it applies to the API as well as the workers.
Correct for a worker draining a long run; it makes every API rollout
potentially half an hour slower.

*~15m.*

---

## Unproven — controls with no mutation behind them

`scripts/mutation_check.py` covers 47 controls. These are the modules it does
not touch. Each has property tests; nothing shows those tests would fail if the
control were removed.

### 4. Telemetry scrubbing and pseudonymisation — **do this one first**

`observability/telemetry.py` redacts logs and HMACs tenant labels. This is a
privacy boundary whose failure is silent and irreversible: leaked telemetry
cannot be recalled. The readiness-probe DSN leak fixed in Phase 15 was exactly
this class of bug and shipped in the previous tree. *~2h.*

### 5. Cooperative cancellation

`correctness/cancellation.py`. Nothing shows the test would fail if
`cancellation_point` stopped raising. *~1h.*

### 6. Checkpoint immutability and ordering

`adapters/postgres/checkpoint.py` enforces step ordering and refuses
conflicting rewrites. Both asserted, neither mutated. *~1h.*

### 7. Judge and metric scoring

`gates/judge.py` and `gates/metrics.py`. Existing mutations cover the promotion
gate that *consumes* the numbers, not the numbers themselves. *~2h.*

### 8. Build bounds and reaping

`corpus/builds.py` enforces at most two coexisting builds — the constraint that
keeps `build_version` post-filtering from re-opening the recall problem
migration 0014 solved. *~1h.*

### 9. One eval run per due window rests on an index alone

Deliberately not mutated: freezing `next_run_at` is caught by the unique index
on `(tenant_id, idempotency_key)`, because the schedule key derives from that
timestamp. Genuine defence in depth — but if the key derivation ever changes,
the protection disappears silently and no test will say so. *~45m for a test
pinning the derivation.*

---

## Unproven — delivery and operations

### 10. Mutation and load suites have never run in CI

Both are gated on `if: github.event_name == 'schedule'` in
`.github/workflows/ci.yml` (Monday 02:17). So **47/47 controls load-bearing is
still laptop-only evidence**.

Because the condition names `schedule` specifically, `workflow_dispatch` will
not run them either — there is currently no way to run the full gate on demand.
Widening that condition is a one-line change worth making early.

### 11. Release workflow and attestation chain

Tag-triggered image build, GHCR push by digest, SBOM and SLSA provenance, and
the OCI descriptor-graph verifier. Never run. Do not tag until this is
deliberate.

### 12. Kubernetes base manifests

Nine manifests, never applied to any cluster. `make policy` validates their
shape; it cannot tell you a NetworkPolicy admits the traffic the app needs or
that KEDA scales. A local kind or k3d cluster proves most of it with no cloud
account. *~1 day.*

### 13. Twenty alerts, nineteen runbooks

Every alert resolves to a checked-in runbook; none has fired or been walked. A
runbook nobody has followed is a document, not a procedure. *~half a day for a
representative three.*

### 14. Production-readiness contract has no drill evidence

`docs/production-readiness.md` requires restore, credential-rotation, erasure,
rollback and canary drills, executed by someone other than their author. Only
the database restore drill has been done. *Environment-gated.*

---

## Absent — evaluation depth

### 15. No cassettes recorded — **keystone of this group**

`evidence/cassettes` holds zero files, so `cassette_mode=replay` refuses to
start by design. Without cassettes the eval gates cannot run deterministically
and load tests cannot run free, which is why items 16–19 are all stalled. *~2h.*

### 16. `requires_kg_hop` is recorded but never sliced

The flag is stored per item and counted in a summary, but no metric splits on
it — so *does graph mode beat dense retrieval on the questions that need a
hop?* is unanswered, despite the data already being collected. **Highest
insight per hour in this document.** *~half a day.*

### 17. The judge is single-shot

No self-consistency check. One sample from one model decides whether an answer
passed; disagreement between runs is invisible. *~half a day.*

### 18. `entity_hints` produced and never consumed

`workloads/onboarding/workload.py` writes them; nothing reads them. *~2h to
wire in or delete.*

### 19. No coverage check before drafting an eval set

The equivalent today is reading `fix_surface` after a run has already spent
tokens. *~half a day.*

### 20. Nothing mines chat history for real questions

Eval sets are seeded from the corpus, not from what users actually asked. The
gap backlog exists and is retained; nothing turns it into questions. *~1 day.*

---

## Absent — corpus and storage

### 21. Object bytes are never reclaimed

`delete()` exists on the adapter; no sweep finds objects whose rows are gone.
Storage grows monotonically with every replaced document. *~half a day.*

### 22. Object isolation is application-level only

Keys are derived and re-checked on every operation, but there is no per-tenant
bucket policy. A bug in key derivation is the only thing between tenants —
unlike Postgres, where RLS backstops the application. *~1 day.*

### 23. Rebuilds copy every chunk row forward

Correct and increasingly expensive. Fine at demo scale; the first thing to hurt
at real corpus size. *~1 day.*

### 24. No binary document support

`SUPPORTED_SUFFIXES` is `.txt .md .csv .html`. PDF, DOCX and XLSX are refused
at upload — deliberately, since they were previously accepted and silently
unindexable. Real corpora are mostly PDF, so this is the largest functional gap
for actual use. *~1–2 days per format family.*

---

## Actions

### 25. Rotate two OpenAI keys — **highest priority, unblocks 26**

`local-platform/.env` ends `…5JqdwA`; `local-platform-codex/.env.local` ends
`…K-DvAA`. Different keys, both live-looking, both on disk. Deleting a file is
not revoking a credential.

### 26. Delete `local-platform/`

Fully superseded: its `ROLLOUT.md` is contained in this tree with zero unique
lines, and `.env` is its only unique file. 866 MB. Rotate first, or you lose
the ability to identify which dashboard entry to revoke.

### 27. Clear the foreign token from `~/.git-credentials`

It stores a PAT belonging to `rajeshthokala10` under Anil's username — the
cause of the initial push 403. This repository uses SSH and is unaffected;
other HTTPS repositories on that machine are not.

---

## Roadmap

Six stages ordered by dependency, not by size. Each stage's gate must hold
before the next is worth starting.

### Stage 1 — Make the ground safe · **gate met**

CI green on `main` at `b2af317`. It earned its place immediately: the first run
found a stale Phase 15 claim within seconds — `pip-audit --strict` had been
auditing the installed environment, which since the packaging fix includes this
unpublished project, so the PyPI lookup failed and `--strict` promoted it to a
build failure. No local run could have caught it.

**Still open:** items 25 and 26.

### Stage 2 — Close the three defects

Items 1–3, plus widening the CI condition in item 10 so the full gate can be
dispatched on demand. Under two hours in total.

*Gate: graph mode refuses cleanly when disabled, and the refusal is a property
test.*

### Stage 3 — Finish the mutation set

Items 4–9, telemetry scrubbing first. This applies the project's own standard
uniformly for the first time. Expect it to find at least one hole — the last
round did, and the hole was the single-use property of tool approvals.

*Gate: every control module has at least one mutation; the run stays at 100%.*

### Stage 4 — Record cassettes, then measure

Item 15 first; it unblocks 16–19. The `requires_kg_hop` slice is the highest
insight per hour available, and it uses data already being collected.

*Gate: the eval gate runs end to end at zero token cost.*

### Stage 5 — Prove the operations layer

Items 12 and 13 on a local kind or k3d cluster — no cloud account needed, which
matters given the project's founding zero-credit constraint. Then, and only
after CI is durably green, cut `v0.1.0` and let item 11 prove the attestation
chain.

*Gate: a rendered overlay passes `check_deployment_policy --release` and runs.*

### Stage 6 — Pay down corpus debt

Items 21–24. Last not because it is unimportant but because none of it is a
correctness or trust problem — it is what starts to hurt when the platform
carries a real corpus rather than a demo one. PDF support (24) is the item most
likely to be pulled forward by an actual user; do that deliberately rather than
drifting.

*Gate: storage stops growing monotonically; a real document set ingests.*

---

## What none of this changes

Closing every item above still does not make an arbitrary environment
production-ready, and `docs/production-readiness.md` is right to say so. Launch
needs managed multi-zone data stores with PITR, real secret-manager bindings,
private endpoints, alert routing that reaches a human, and drills executed by
someone other than their author. Stages 1–6 close what the codebase can close
on its own; the rest is environment, and it is honest that it stays open.
