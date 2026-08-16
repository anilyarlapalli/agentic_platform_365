# Rollout log

The running record of this deployment: what was built, what broke, what was
learned, and what is blocked. Written as it happens, not tidied afterwards — a
log that only records successes is a log that cannot be used to debug anything.

Conventions:

- **Failure** — something that did not work, with the cause once known.
- **Learning** — something true that was not obvious, worth not rediscovering.
- **Blocker** — something stopping progress, with who or what can clear it.
- **Overclaim** — something reported as done that was not fully done. Recorded
  explicitly because a silent correction is how a status report stops meaning
  anything.

---

## Context

| | |
|---|---|
| Started | 2026-08-12 |
| Goal | Run the production disciplines locally, on zero Azure credits |
| Reference | `../graphrag-azure/` — read-only, never modified |
| Engine | `/home/anil-y/app_ideas/manufacture/R_repo/AgenticAI_Manufacturing` @ `anil_develop`, 17 schemas — read-only import |
| Framing | The RAG is the fixture; the platform is the product |

### Locked decisions

| Decision | Choice | Why |
|---|---|---|
| Engine tree | canonical source, not the portfolio copy | `ROLLOUT14.md` Traps: building from `azure_deployments/graphrag-azure/` silently drops 14 of 17 domains |
| Queue | Celery/Redis + transactional outbox | Matches the production stack, and the outbox is the mechanism area 2 needs |
| Vectors | pgvector with row-level security | Isolation enforced by the database, not by application code that can be bypassed |
| LLM | OpenAI direct, cassette record/replay for gates and load | Gates must be deterministic; a 1,000-request load test against live gpt-4o costs real money |
| Budget on unreadable ledger | **per-task policy** — interactive fails open, background fails closed | Decided 2026-08-12, see below |

---

## Phase 0 — Foundation

**Delivered.** Substrate up, RLS enforced, 9 property tests green, 4/4 mutations
caught.

Ports offset so this coexists with the unrelated `storiesai` stack already
holding 5432/5433, 6379, 8080, 11434, 11435:

```
postgres 5442 · redis 6389 · minio 9100/9101
jaeger 16687 · prometheus 9190 · grafana 3100 · otel 4317/4318
```

### Failure · `SET LOCAL` cannot take bind parameters

First run of the isolation suite: 6 errors, 2 failures, on
`syntax error at or near "$1"` for `SET LOCAL app.tenant_id = $1`.

`SET` is a *utility* statement, not a plannable one, so Postgres does not accept
bind parameters in it. The tempting fix is to interpolate the value into the SQL
string — which places a caller-supplied value into the body of the single
statement that every other isolation guarantee depends on. That is an injection
point on the control itself.

**Fix:** `SELECT set_config(:guc, :tenant_id, true)`. An ordinary function call,
so the value binds normally, and `is_local => true` gives exactly `SET LOCAL`
semantics — reverted at COMMIT or ROLLBACK, never carried back into the pool.

**Learning:** any GUC that carries a security discriminator has to be set through
`set_config`, never through `SET`. The statement form forces a choice between a
syntax error and an injection.

### Failure · the mutation harness was wrong, not the test

`scripts/mutation_check.py` reported `policy-without-with-check` as **NOT
CAUGHT** — dropping `WITH CHECK` from the chunk policy left all 9 tests green.
Initial read: a gap in the test suite.

Wrong. For a `FOR ALL` policy, Postgres **reuses the `USING` expression as the
post-image check** when `WITH CHECK` is omitted. So the mutation did not create a
vulnerability, and the suite was correct to stay green.

The genuinely dangerous form is an explicit permissive clause — `WITH CHECK
(true)` — which accepts any post-image. Mutation rewritten to that; now caught.

**Overclaim corrected:** the migration docstring stated that omitting `WITH
CHECK` lets a tenant write across the boundary. That was backwards. Fixed in
`0001_foundation.py`.

**Learning:** a mutation harness tests the tests, and it will sometimes be the
thing that is wrong. "NOT CAUGHT" means *either* the test is inadequate *or* the
mutation is not actually a vulnerability — and distinguishing those is the whole
value of running it.

### Failure · package named `platform` shadows the stdlib

`platform_core/` was originally `platform/`, which shadows Python's stdlib
`platform` module — imported by `openai` and `httpx` for user-agent
construction. Caught before the first import; renamed.

**Learning:** cheap to fix at minute five, expensive at hour three when the
traceback points somewhere unrelated.

### Overclaim · Phase 0 reported complete without the ports

The Phase 0 task listed "the six ports (ObjectStore, JobQueue, VectorIndex,
Ledger, CheckpointStore, LLMClient)". `platform_core/ports/` shipped as an empty
package — only `__init__.py`. The isolation work was real and the tests are real,
but the port layer was not written and the phase should not have been marked
complete.

Written at the start of Phase 1, where they are needed anyway. Recorded rather
than quietly backfilled.

### Learnings kept

- **Two database roles is the control, not a convention.** `platform_app` is
  `rolsuper=f, rolbypassrls=f`. Either flag would leave `pg_policies` fully
  populated while no policy applies — a control that looks present in every
  inspection and enforces nothing. Asserted in
  `test_application_role_cannot_bypass_rls`.
- **Fixtures must seed two tenants with near-identical data.** Distinct data
  lets a broken boundary pass by coincidence. The seeded embeddings differ only
  in the first component, so an unscoped vector search returns a foreign row at
  almost equal distance — a *wrong answer*, not a missing one.
- **The pooling trap needs a one-connection pool to surface.** With
  `is_local=false` the tenant GUC survives into the next checkout. Single-tenant
  testing never sees it. `test_tenant_context_does_not_survive_into_the_pool`
  alternates tenants over a shared pool specifically to force it.

### Open questions

- **Budget fail-closed default.** Flagged to the user, not yet answered.
  Currently `budget_fail_closed=true`.

---

## Phase 1 — Identity and tenant isolation

**Delivered.** 19 property tests green, 7/7 mutations caught. 16 routes, all
with a declared authorisation policy.

Ports written first, closing the Phase 0 overclaim: `ObjectStore`, `JobQueue`,
`VectorIndex`, `Ledger`, `CheckpointStore`, `LLMClient`, plus a port-level error
taxonomy where `TransientError` makes retryability a *type* rather than a
comment.

### Failure · the authorisation middleware was a silent no-op

The worst bug so far, and it passed every functional test.

`app.py` enumerated `app.routes` filtering for `APIRoute`. Under **FastAPI
0.141.1 / Starlette 1.6.0**, included routers are wrapped in a private
`_IncludedRouter` and are *not* flattened into `app.routes`. So:

* the startup check for undeclared routes iterated an empty list, found no
  violations, and **passed**;
* `_match_route` matched nothing, so every request fell through to `call_next`
  — the entire default-deny layer was inert.

Nothing about this was visible from behaviour. The service started, routes
worked, responses were correct. It surfaced only because a debug line printed
`0 routes registered`, and that number was only being printed at all by
accident.

**Fix:** `platform_core/api/route_table.py` builds the table from `ROUTERS` —
the same tuple `create_app` includes — using the public `APIRouter.routes`,
which already carries the prefix and a compiled path regex. No private
attributes, and the served set and the checked set are the same objects by
construction. `build_route_table()` raises on an empty result rather than
returning one.

**Learning:** an authorisation layer that fails *open* when its input is empty is
the wrong shape regardless of the framework bug. Any check whose "pass" and
"nothing to check" states are indistinguishable needs a non-empty assertion. The
`empty-route-table` mutation now guards it.

### Failure · circular import between the app factory and its dependency

`get_context` lived in `app.py`; route modules imported it; `create_app`
imported the route modules. The symptom depended on import order — `ImportError`
when routes were imported first, and an app with **zero routes and no error**
when `app` was imported first.

**Fix:** dependencies moved to `platform_core/api/deps.py`, a leaf that imports
neither the app nor the routes.

**Learning:** this is the same failure mode as the Azure build's `bootstrap.init`
ordering ritual — behaviour that depends on which module is imported first, with
the constraint recorded only in prose. Structure it so the cycle is impossible
rather than currently-absent.

### Failure · second stale mutation, same class as Phase 0

`self-approval-permitted` reported NOT CAUGHT. The mutation was
`frozenset(set()) or frozenset({...})` — an empty frozenset is falsy, so `or`
returned the original set and the mutation did nothing.

`_patch_file` now asserts the target appears **exactly once** and raises
otherwise, so a stale mutation is an error rather than a false gap report.

**Learning:** twice now, "NOT CAUGHT" meant the mutation was wrong rather than
the test. The harness needs the same scepticism as the code it checks.

### Learning · PyJWT flagged a genuinely weak default

`InsecureKeyLengthWarning`: the development `jwt_secret` was 21 bytes, under RFC
7518 §3.2's 32-byte minimum for HS256. Now enforced by a validator at *every*
environment, not just production — a development default that is illegal is a
default someone eventually ships. `jwt_algorithm` is also pinned to a `Literal`,
and `decode_token` passes a fixed algorithm list so `alg: none` cannot be
honoured.

### Design notes worth keeping

- **Capabilities, not roles, at every call site.** Roles map to capabilities in
  one table; grants can also be resource-scoped with an expiry. The Azure build
  needed exactly this and reached it as an exception — a per-domain reviewer
  table added when `admin` proved too coarse for schema review.
- **404, never 403, for another tenant's identifier.** A 403 confirms the id
  exists. `NotFoundError` in the port layer deliberately does not distinguish
  "absent" from "not yours" for the same reason.
- **Deny by default means an undeclared route is refused**, not permitted. The
  boot fails if any served route has no policy; the middleware refuses again at
  request time in case one is added after boot.
- **Maker-cannot-be-checker is enforced twice** — a capability rule for a clean
  403, and a CHECK constraint in migration 0002 that no code path can bypass.

### Resolved · budget behaviour when the ledger is unreadable

Carried from Phase 0, decided 2026-08-12: **per-task, not a global flag.**

The question is narrower than it looks. Over-budget always refuses; this governs
only the *unknown* state, where the ledger cannot be read at all.

What made it non-obvious: `check()` and `record()` share one Postgres, so the
outage that hides the ceiling also swallows the writes. Failing open therefore
produces spend that is **uncapped and unrecorded** — the ledger ends up with a
hole exactly the size of the outage, unreconstructable afterwards. That is worse
than "the budget was briefly not enforced".

Why the Azure build's opposite choice is right *for it*: `ARCHITECTURE.md`
forbids Postgres on the retrieval hot path, so a Postgres outage does not stop
chat answering. Failing closed there would take a working service down to
protect a budget. Here every read goes through `tenant_session`, so Postgres
being down already means nothing can be served and failing closed costs nothing.

The decided policy splits on blast radius, which differs by orders of magnitude:

| Path | Behaviour | Why |
|---|---|---|
| chat, query | fail **open** | one call, cents, a user is waiting |
| ingest, onboarding, eval | fail **closed** | thousands of calls, nobody waiting — the "month of budget in an afternoon" case |

`RequestContext` already carries the task label, so this is policy in the ledger
adapter rather than a global setting. To be built in Phase 3.

**Revisit if** a read path ever moves off Postgres (a Redis-cached query path,
say) — the availability argument that justifies fail-open in the Azure build
would then start applying here too.

---

## Phase 2 — Checkpoint, queue and side-effect correctness

**In progress.** Schema and the cross-tenant credential are done and proven —
21 property tests green, 7/7 mutations caught. Runtime outbox relay, lease
heartbeat, reaper and the chaos harness are still to come.

Migration `0003` adds `outbox` (intent committed with the state change),
`side_effect` (idempotency ledger keyed `(run_id, step)`), a heartbeat column on
`run`, and partial indexes for the claim and reap queries.

### Failure · I opened a cross-tenant hole, and the suite did not notice

The worst kind of mistake: made while building the isolation machinery, in the
isolation machinery.

The relay and reaper genuinely need cross-tenant reach — one relay drains every
tenant's outbox. The first version of `0003` expressed that as an RLS policy
keyed on a **session variable**: any connection that set `app.system_reason` and
no tenant could read across the boundary.

`system_session()` is also used by the readiness probe and by
`auth.authenticate()`'s tenant lookup. So the moment those policies existed, a
session opened by **login** could read every tenant's runs. Measured directly
before reverting:

```
runs visible to the AUTH path's system_session: 2    ← two tenants
```

**The defect was the shape, not the expression.** A privilege that any caller
can grant itself by passing a string is not a boundary. Rebuilt as a dedicated
`platform_relay` login role: cross-tenant on exactly `outbox`, `side_effect`
and `run`, denied everywhere else, and the API process is configured only with
the `platform_app` DSN so it cannot authenticate as the relay however it is
called.

```
runs visible to the AUTH path's system_session: 0    (was 2)
runs visible to platform_relay:                 2
relay reading document:                         denied
```

**Learning:** a privilege boundary must be something a process either holds or
does not hold. If it can be assumed by setting a variable, every code path that
can set that variable is inside the boundary — including the ones you forgot
were there.

### Failure · the fail-closed test passed vacuously the whole time

`test_missing_tenant_context_returns_zero_rows` iterated every tenant-scoped
table asserting `count(*) == 0`. But its fixture only seeded `document` and
`chunk`, so `run`, `outbox` and `side_effect` were **empty** — and `count == 0`
is true whether the policy blocked the read or there was nothing to read.

It therefore passed, green, while the hole above was wide open in exactly those
three tables.

**Fix:** a `populated_every_table` fixture seeds at least one row per tenant in
*every* tenant-scoped table and asserts the seeding worked before the test runs.
Two new properties added: the relay reaches only its three tables, and
`platform_app` is not a member of `platform_relay` and cannot `SET ROLE` to it.

**Learning — the generalisable one:** an assertion whose "pass" state is
indistinguishable from its "nothing to check" state is not an assertion. This is
the *second* instance in two phases: the empty route table in Phase 1 had the
identical shape. Any check over a collection needs a non-empty precondition.

### Learning · the downgrade path got its first real exercise

Reverting `0003` to rebuild it ran `alembic downgrade 0002` against a populated
database. It worked cleanly — policies dropped, tables dropped, columns removed,
no leftovers. Unplanned, and worth more than a rehearsed test would have been:
the down path was exercised because something was actually wrong.

### Chaos results — SIGKILL at every boundary

12 crash points, each a real `SIGKILL` in a subprocess. Every one recovers:

| crash point | after reap | final | notifications | incomplete step |
|---|---|---|---|---|
| after:lease | pending | succeeded | 1 | — |
| before/mid/after:reserve | pending | succeeded | 1 | — |
| before/mid/after:transform | pending | succeeded | 1 | — |
| before:announce | pending | succeeded | 1 | — |
| **mid:announce** | pending | **failed** | **1** | **announce** |
| after:announce | pending | succeeded | 1 | — |
| before/after:complete | pending/succeeded | succeeded | 1 | — |

`mid:announce` is the one that matters: the effect landed, was never recorded
complete, and is **not** safe to repeat — so the run stops terminal with a
reconciliation reason and the step is left precisely described. That is the
Azure publish-then-finish gap, handled.

### Failure · the chaos harness was vacuous, third instance of the same shape

The first version crashed only at `before:` and `after:` each step — which
bracket the whole `perform_once` call. Those only ever produce "not claimed" or
"claimed and completed", both trivially recoverable. **The dangerous window —
claimed but not completed — is *inside* `perform_once` and was never hit.** So
`NEEDS_RECONCILIATION` never executed and the "no double application" assertion
passed for the wrong reason.

Added `mid:<step>`, which fires between the effect landing and the completion
write. That is where the retry policy actually decides anything.

**This is the third time in three phases:** empty route table (Phase 1), the
fail-closed test over empty tables (Phase 2), and now this. The pattern is
always *an assertion whose pass state is indistinguishable from its
nothing-to-check state*. Rule adopted: every such check needs an explicit
precondition — non-empty collection, crash actually fired, table actually
populated. The chaos harness now asserts `returncode == -9` for exactly this
reason.

### Failure · concurrent attempts double-executed a side effect

Found by a test written *because* the mutation harness flagged
`side-effect-uniqueness-dropped` as NOT CAUGHT.

`perform_once` read every `started` row as "a previous attempt died", so a
second concurrent attempt re-ran an effect that was **still in flight**. The
test observed the effect body execute twice for one `(run_id, step)`.

The two cases need opposite handling and were collapsed into one:

| row state | means | correct action |
|---|---|---|
| `started`, claim live | another attempt is running now | back off |
| `started`, claim expired | a previous attempt died | apply retry policy |

Migration `0004` adds `claimed_by` / `claim_expires_at`. Note what the unique
constraint does and does not buy: it makes the **row** unique, not the
**execution**. Uniqueness cannot distinguish a live claimant from a dead one —
that needs a deadline, exactly as the run lease does.

### Failure · the fix deadlocked recovery, caught immediately by the chaos suite

Adding the claim deadline broke all three `mid:` crash tests. A crashed worker
left a claim live for 5 minutes while its **run lease** expired in seconds — so
the run returned to `pending`, the next worker picked it up, and was refused by
a claimant that no longer existed. Recovery deadlocked until the longer TTL
lapsed.

A run lease and a step claim are two grips held by one process with independent
deadlines. The reaper is the single recovery mechanism, so it now releases both.
It cannot touch a live worker's claims: a run only becomes a candidate once its
lease has already expired.

**Learning:** two independent expiries on one process's grips will disagree, and
the shorter one is what recovery observes. Whatever releases one must release the
other.

### Failure · a synthetic identity is fine until something references it

The worker's principal was `UUID(int=0)`. That violated the foreign key on
`document.uploaded_by` the moment a workload wrote anything attributed to it.
Now a real per-tenant `service:worker` principal row, created on first use — so
audit rows join to something that exists, and "alice ingested" stays
distinguishable from "the worker ingested for alice's tenant".

### Learning · SQLAlchemy bind syntax collides with Postgres casts

`:workload::text` parses as a bind parameter named `workload:`. Use
`CAST(:workload AS text)`. Same family as the `SET LOCAL` finding in Phase 0:
Postgres syntax and SQLAlchemy's parameter syntax overlap in more than one place.

### Learning · a constraint tested only by a race is not tested

`side-effect-uniqueness-dropped` stayed NOT CAUGHT even after the concurrency
test existed, because with the claim deadline in place the second caller almost
always *sees* the committed claim and backs off — the interleaving where the
constraint decides is real but narrow. A test that only fails on a lucky
scheduler is not verification. Replaced with a direct assertion that the
constraint exists and rejects a duplicate, in the layer that actually holds the
guarantee.

### Transport — delivered and verified against the real broker

`apps/relay/main.py` (daemon), `platform_core/adapters/local/celery_queue.py`
(the `JobQueue` port over Celery), `apps/worker/tasks.py` (task + beat sweeper).

Verified end to end with a real Redis broker and a real worker process —
`scripts/e2e_transport.py`, because the pytest suite runs Celery eagerly and
therefore never exercises the broker at all:

```
admitted run 84e12e54… (created=True)
outbox backlog before relay: 1
relay published: 1
final run status: succeeded
  step reserve:   completed (attempt 1)
  step transform: completed (attempt 1)
  step announce:  completed (attempt 1)
outbox backlog after: 0
```

**The division that makes Celery's guarantees sufficient:** Celery *delivers*,
Postgres *holds*. A pointer carries no authority to execute — the lease decides.
So a duplicate delivery loses the lease race, a lost delivery is recovered by
the beat sweeper, and neither is a correctness problem.
`test_delivery_is_a_hint_not_an_authorisation` asserts exactly this, and it is
what makes eager-mode tests a fair substitute for broker chaos.

### Failure · relay could not read `tenant`

`0003` granted the relay its three tenant-scoped tables and nothing else — right
instinct, one table short. Both the relay and the task handler must turn a
`tenant_id` into a `(id, slug)` before they can open a scoped session at all.
`permission denied for table tenant`.

`0005` grants **SELECT only**, and explicitly revokes the writes: creating a
tenant mints an isolation scope, updating one moves budget ceilings. The test
now asserts the read/write asymmetry rather than merely that the read works.

### Failure · the e2e script deadlocked on a pipe

First run hung for five minutes with no output. Not the broker: `subprocess.PIPE`
on a chatty Celery worker fills the 64KB buffer, the worker **blocks on write**,
and nothing progresses. Redirected to a file.

**Learning:** a hang that looks like a distributed-systems problem is worth
checking against local plumbing first. Redis, the Celery CLI and `send_task` were
all verified working in under a minute, which located it immediately.

### Failure · `pkill -f` killed my own shell

`pkill -f "celery -A apps.worker.tasks"` matched the bash process whose command
line *contained* that string — the shell running the pkill. Exit 144. Kill by
PID, or match on something that cannot appear in the invoking command.

### Phase 2 closed

48 property + chaos tests green, 11/11 mutations caught, real transport verified.

## Phase 3 — Tracing, audit and cost attribution

**Delivered.** 59 tests green, **16/16 mutations caught**, verified with a real
OpenAI call.

Migrations `0006`–`0009`: `llm_usage` (tenant_id **NOT NULL**), `audit_event`
(append-only, hash-chained per tenant), explicit audit purge, run-id correlation,
cost precision.

### The headline number

```
unattributed spend: 0
```

Structural, not aspirational: `llm_usage.tenant_id` is `NOT NULL`, every call
takes a `RequestContext`, and `_identity` refuses a call that has none. There is
no `"unknown"` to fall back to. The Azure equivalent cannot be zero — its
ContextVar defaults to the string `"unknown"` and is set on the ingest and eval
paths only, so chat and onboarding both bill to it, and because it is never reset
a worker that ran an ingest bills a later onboarding step to that domain.

### One chain, one order, asserted

    identity → budget → cache → retry → dispatch → meter → trace

Replaces four independently-applied wrappers whose relative order is decided by
`bootstrap.init`'s call sequence. `CHAIN_ORDER` is a tuple a test asserts, with
the reasoning attached: budget before cache (a stampede of misses must not
bypass the ceiling), retry inside budget (N attempts at one logical call are
authorised once), meter after dispatch (usage is only knowable from the
response).

### Failure · the ledger silently dropped a real charge

First live call: the answer came back correctly, and the ledger row **was never
written**. `llm_usage.run_id` had a foreign key to `run`, but an interactive
chat request is not a queued run — `RequestContext.run_id` is the unit of work,
which exists whether or not anything was queued. FK violation.

The behaviour around it was all correct, which is the interesting part.
`Ledger.record` deliberately swallows its own failures — losing a ledger row must
not lose the answer that was already paid for — so it logged at ERROR and
returned. That is the right trade, and it is precisely why the live check exists:
**a subsystem designed not to fail loudly needs something that inspects the
outcome rather than the return value.**

`0008` makes `run_id` a plain indexed correlation column.

### Failure · six decimal places is not enough for money

```
computed: $0.00000240
ledger:   $0.00000200     ← NUMERIC(12,6)
```

A 17% under-count on every sub-cent call — and cheap models are used in the
highest volume, so the error concentrates exactly where the call count is
largest. A million such calls is $2.40 spent, $2.00 recorded, and nothing
reports a discrepancy.

Found only because `scripts/e2e_llm.py` prints the computed cost and the stored
cost next to each other. Neither number looked wrong alone. The property suite
missed it because its fixtures use round numbers that survive the rounding.

`0009` widens to `NUMERIC(20, 12)`. `NUMERIC` rather than float throughout:
binary floating point cannot represent most decimal fractions, and summing
millions of tiny float costs accumulates error in a figure meant to reconcile
against an invoice.

**Learning:** a value that is *plausible* is not a value that is *correct*. Test
money against an independently computed figure, not against a range.

### Failure · append-only vs. tenant deletion

`0006`'s trigger blocked DELETE, so cascade-deleting a tenant failed outright —
the audit rows refused to go. Not a test artifact: it is the real tension between
tamper-evidence and erasure obligations.

`0007` resolves it explicitly. UPDATE is refused unconditionally (correcting the
record means appending a correction). DELETE requires the session to declare
`app.audit_purge_reason`, which the trigger writes to the server log before
allowing the row to go.

Note the difference from the mistake in `0003`: that GUC was the *sole* thing
standing between the app role and every tenant's data. This one is a declaration
of intent that cannot grant a privilege — the app role holds no DELETE on
`audit_event` at all, so it only unlocks anything for the owner.

### Learning · privilege mismatches surfaced twice more

`verify_chain` reached for the relay credential when it is per-tenant by
construction and needs none. `unattributed_spend` did the same for a query that
is an operator concern, not a delivery one. Both narrowed. The pattern to watch:
reaching for the widest credential that works, rather than the narrowest that
does.

## Phase 4 — Evaluation and production-quality gates

**Delivered.** 69 tests green, **20/20 mutations caught.**

### The acceptance case

```
baseline recall 1.00 → candidate recall 0.40 (Δ -0.60)
BLOCKED: retrieval_recall fell 0.6000, beyond the 0.0200 tolerance
baseline pointer unmoved
```

A deliberately degraded retriever — one that quietly gets 40% right rather than
failing outright, because that is the realistic regression — is refused, and the
baseline does not move.

### What makes it a gate rather than a measurement

`eval_run` accumulates; `eval_baseline` is a **pointer**. Promotion moves the
pointer and never destroys the run it moved away from, so a regression stays
inspectable. The Azure build writes to one blob per domain that the next run
overwrites, which makes "did this regress?" unanswerable by construction — a
number with no history is a reading.

Four refusals, each separate because each hides a different wrong answer:

| refusal | why it is not paranoia |
|---|---|
| regression beyond tolerance | the obvious one; two thresholds, not a combined score |
| **incomparable `dataset_sha`** | a perfect 1.0 on *different questions* is real, correctly computed, and means nothing next to the baseline |
| shrunken sample | 2 scoreable items can beat 25 on average while being worse |
| **unscoreable run** | a run producing no metric must not read as "no regression detected" |

That last one is the vacuity failure this codebase has now hit three times in
other forms, anticipated rather than discovered.

Datasets are content-addressed: editing a question forks a version, reordering
does not, and non-canonical `must_cite` entries are refused at construction — the
Azure eval sets carry a synthetic `page:…` handle the retriever can never emit,
which scores a permanent miss indistinguishable from a real retrieval failure.

`force` exists and is audited **with the reasons it overrode**. A gate with no
override gets bypassed by deleting the baseline, which loses the history.

### Failure · the mutation harness timed out and left a control disabled

The check grew to 20 mutations × a 33-second suite ≈ 11 minutes, hit a 10-minute
timeout, and was killed **mid-mutation** — leaving
`bad = []  # mutation: citation format unchecked` applied in `datasets.py`. The
suite would still have been green, because that mutation is caught by a fast
property test the killed run never reached.

Two fixes:

**Mutations declare their suites.** Only the three correctness controls need the
32-second chaos run; the rest are caught by property tests in about a second.
Total time fell from ~11 minutes to ~3. A check nobody runs because it is slow is
a check that is not run.

**The harness refuses to start on a dirty tree.** `_leftover_markers()` greps
every mutable file for `# mutation:` and aborts if any remain.

**Learning:** a tool that deliberately breaks the code must assume it will be
killed while the code is broken. Recovery cannot depend on its own `finally`.

## Phase 5 — Release, load, chaos and scaling

**Delivered.** 85 property + chaos tests, 4 load tests, **23/23 mutations
caught.**

### The acceptance case

```
canary rev-0002-bad at 10%   (split: good 90 / bad 10)
baseline error rate 1%  →  candidate 33%
ROLLBACK: error rate rose 32.50%, beyond the 5.00% tolerance
traffic restored: {rev-0001-good: 100}
```

No human, no redeploy. The previous revision never stopped running, so restoring
traffic is a weight change rather than a rebuild — which is the whole difference
from `az containerapp update --image` in single-revision mode, where recovery
means rebuilding the previous tag while the bad revision keeps serving.

### Comparative, not a fixed threshold

The gate judges a canary **against the revision it replaces, over the same
window**. A fixed threshold is wrong in both directions: it fires on every deploy
of a service with a 2% steady-state error rate, and never fires on a quiet one
that regresses a hundredfold. `test_the_gate_compares_against_the_baseline`
asserts that two revisions both at 10% errors is *not* a regression.

An undersampled canary is **held**, not promoted — the same vacuity rule as
everywhere else. A perfect score over three requests means nothing.

### Rollback reports whether the schema must move

Restoring an image does not restore the database. A candidate whose
`schema_version` differs from the active one cannot be recovered by traffic
alone, and the rollback says so loudly rather than reporting success. Expand/
contract is what makes the answer normally "no".

### Sessions and cache

Sessions are rows, bound to `(tenant, principal)` — so any replica serves any
turn, and a session id is **not** a credential. Turn append is a single atomic
SQL statement, because read-modify-write in the application loses a turn whenever
two messages land on different replicas.

Cache keys are **prefixed** with the tenant rather than checked after the fetch:
a cross-tenant hit is unreachable rather than merely rejected. Every cache
failure degrades to a miss — a cache that can take the request path down is a
dependency pretending to be an optimisation.

### Measured, not asserted

```
queue drain      200 runs / 8 workers → 161 runs/s, p95 55ms, 0 duplicate steps
lease contention 50 runs / 16 workers → 1,143 acquisitions/s, 0 double-leases
reaper           100 stranded runs    → recovered in one pass, 11ms
isolation        2 tenants interleaved through one pool → 0 cross-tenant reads
```

Absolute latency on a laptop sharing Postgres with an unrelated stack is not a
number to gate on, so the load tests **assert completeness and isolation** and
**record** timings to `evidence/load/` for trending.

### Failure · the SQLAlchemy cast collision, third occurrence

`:turn::jsonb` in the session append — the same bug as `:workload::text` in
Phase 2 and `SET LOCAL app.tenant_id = :x` in Phase 0. Three times in one build.

Rather than fix it a third time and move on, `tests/properties/test_sql_hygiene.py`
now fails the build on the pattern.

### Failure · the hygiene check flagged its own documentation

The first version grepped lines and immediately failed — on the docstrings that
*describe* the anti-pattern. A line scanner cannot tell
`SET app.tenant_id = '<uuid>'` written as an example of what not to do from the
same text as executable SQL.

Rewritten to walk the AST and inspect only **non-docstring string literals**.
Comments are structurally absent from the AST, so documentation cannot trip a
rule about what it documents — which means the rule can be explained honestly in
the module that enforces it.

It carries its own effectiveness test: a sample file containing both a real
violation and a docstring describing one, asserting the scanner sees exactly the
first. Without that, a scanner that matched nothing would pass silently — the
vacuity failure, anticipated this time.

---

## Build summary

| | |
|---|---|
| Phases | 6 of 6 complete |
| Migrations | 11, each with an exercised downgrade |
| Tests | 85 property + chaos, 4 load |
| Mutations | **23/23 caught** — every control is load-bearing |
| Verified against | real Redis, real Celery worker, real OpenAI API |
| `graphrag-azure/` | **0 files modified** |

### The recurring lesson

Five separate instances of one shape:

1. an empty route table → authorisation middleware silently permitted everything
2. a fail-closed test over empty tables → passed while a cross-tenant hole was open
3. a chaos harness crashing only *outside* the dangerous window
4. a mutation that matched nothing, reported as a test gap
5. a SQL scanner that would have passed by scanning zero files

**An assertion whose pass state is indistinguishable from its nothing-to-check
state is not an assertion.** Every collection check in this codebase now carries
an explicit precondition: non-empty table, crash actually fired, scanner actually
matched, sample actually large enough.

---

## Phase 7 — the operator console (2026-08-13)

Three pages under `web/`, plus the onboarding subsystem they needed. The UI was
the ask; roughly two thirds of the work turned out to be backend, because
`admin/onboard` is a client for a capability this platform did not have.

### What was ported, and what was not

`graphrag-azure/web/` is ~4,265 lines of Next.js. Its client calls ~30
endpoints; only two matched here by name — and not the ones expected. **The auth
and health routers carry no `/api` prefix**: the real paths are `/auth/login`,
`/auth/me` and `/health`, and `PUBLIC_PATHS` lists them in that form. An early
note in this session claimed `/api/auth/login`; that was wrong and would have
produced a 404 that reads like a missing feature.

Dropped deliberately: the two voice hooks (`useVoiceInput`, `useVoicePlayback`)
and the `/api/voice/*` calls. There is no counterpart here, and shipping a
microphone button that always fails is worse than not shipping one.

Rewritten rather than ported: the chat page. The Azure UI has no concept of
`mode`, per-source `signals`, `edgeless`, `cache_hit`, or per-turn cost — all of
which this API returns and all of which are now shown.

### Requests are same-origin on purpose

The browser talks to the Next server, which rewrites `/api/*`, `/auth/*` and
`/health` to the API. This is why `create_app` still has no CORS middleware:
same-origin requests raise no preflight, so the default-deny surface needs no
`OPTIONS` exemption. Pointing the browser at `:8100` directly would have
required punching that hole.

### Next was pinned to a vulnerable version

`npm` flagged `next@14.2.5` on install. One advisory —
[GHSA-p9j2-gv94-2wf4](https://github.com/advisories/GHSA-p9j2-gv94-2wf4), SSRF
via rewrites — lands directly on the proxy above. Bumped to `14.2.35` and
`postcss` to `8.5.26`.

**Two advisories remain and are accepted, not fixed.** Both are inside Next
itself and only clear by moving to Next 16 (breaking): a DoS via
`next/image` `remotePatterns`, and Next's own vendored `postcss`. Neither is
reachable — no `remotePatterns` are configured, and the bundled postcss only
processes our own CSS. Revisit on a Next 16 migration.

### The engine tree baseline was recorded wrong

Prior notes said the engine tree had "zero modified files". It does not:
`git status` there shows ~15 modified files dated 2026-08-08 to 2026-08-11,
pre-existing and unrelated. This session added none — verified by mtime, since
everything touched today is newer than that window. `graphrag-azure/` is not a
git repo at all, so `git status` cannot be used on it; check mtimes instead.

### Onboarding: five seams, not three

The relation artifacts are not written by the onboarding API. They are a **side
effect** of the engine's `_step_bootstrap_artifacts`, which resolves paths from
`Path(__file__).parents[N]` inside the engine tree — read-only, and not
tenant-aware. Five seams had to be redirected, all reached through deferred
imports so rebinding the module attribute is observed:

1. `InstanceTable._path_for` — instance table reads
2. `extraction._CACHE_DIR` — the extraction cache (a module constant)
3. `relation_bootstrap.load_predicate_map` — the caller computes this path
   *inline*, so the loader itself has to ignore its argument
4. the `save_to` kwarg on both `bootstrap_*_from_llm_cache` functions
5. **`kg.schema.load_default_schema`** — missed on the first pass. The engine
   resolves schemas through `config.SCHEMA_PATHS`, a dict snapshotted from its
   own tree at import time, so a newly onboarded domain raised
   `unknown domain 'kgdemo'` and the approved taxonomy was unusable by the very
   thing it was drafted for.

The redirect target is **thread-local**. FastAPI runs sync handlers in a
threadpool, so a module-global root would let one tenant's build read another's
instance table — silently, producing a *plausible* graph.

### Onboarding chat was an unmetered side door

`engine.py` routed `encode()` through the instrumented client and nothing else.
The orchestrator makes hundreds of **chat** calls (one per chunk, then eight
synthesis steps) through `core.llm_client.call_llm` straight to OpenAI. Left
alone, the single most expensive path in the platform was the one path a tenant
ceiling could not stop. `install_metered_chat()` patches that one chokepoint —
`llm_router.call` delegates to it and all 26 engine call sites reach it.

Measured after the patch, one draft over 40 chunks:

| model | calls | cost |
|---|---|---|
| gpt-4o (synthesis) | 8 | $0.0411 |
| gpt-4o-mini (extraction) | 6→40 | $0.0011 |
| text-embedding-3-small | 9 | $0.000004 |

All rows carry `workload=onboarding`. Before the patch, none of it existed.

### Artifacts live in Postgres, not object storage

The Azure build stages them to Blob and must: its worker scales to zero, and a
review outlasting the 300s cooldown destroyed the container holding them — one
domain drafted 543 instances and published `relations_available=false`, reported
as success. Here the constraint differs: RLS is the isolation mechanism and
there is no object-store adapter, only the port. Postgres means the artifacts
inherit a boundary that is already enforced and already tested.

### Edges are real now

Two drafts were run live.

**6-chunk demo corpus** → 1 entity type, **0 edge types**, predicate map with
**0 entries**, `relations_available=false`. Not a bug: edge-type synthesis needs
a relation to recur across chunks before promoting it, and a corpus where every
relation appears once is indistinguishable from noise.

**40-chunk corpus** (`scripts/seed_onboarding_corpus.py`, built for recurrence
on purpose) → 5 entity types, **8 edge types**, 36 instances, **11 predicates**,
`relations_available=true`. After approve and publish:

```
graph: {nodes: 52, edges: 2, edgeless: false}
retrieval: {graph_hits: 10, entities_matched: ["F-207", "fault F-207"]}
```

Previously every graph reported `edges: 0`, `edgeless: true`, `graph_hits: 0`.
This is the "known gap" from earlier phases closed: it is traversal GraphRAG
now, not entity-match GraphRAG.

### Caught by the existing suites

* `test_no_bind_parameter_is_followed_by_a_cast` — three `:name::jsonb` binds in
  the onboarding store. SQLAlchemy claims the first colon, so all three would
  have been runtime syntax errors. Now `CAST(:name AS jsonb)`.
* `populated_every_table` — the two new tables had no fixture rows, so the
  isolation assertions over them would have been vacuous. This is the
  "assertions need a non-empty precondition" defect the fixture exists to catch,
  and it caught it.

### Not done

* **No compose service for the console.** `make web` runs it; containerising it
  needs a Dockerfile and a Node build stage, and shipping an unverified service
  definition is worse than shipping none. The API, worker and substrate are
  unchanged.
* Two Next advisories accepted, above.

### Verified

`89 passed` across properties and chaos, and `make mutate` reports **all 23
controls load-bearing, suite restored to green** — re-run after the capability
set changed (`session:read`, `schema:read`) and after the two new tables landed.

One trap worth recording: a **running worker breaks the chaos suite**. Three
`test_crash_at_every_boundary_is_recoverable` cases failed while a
`make worker` process was polling in the background, because it leased the runs
the tests intended to crash and resume themselves. The mutation script refuses
to start on a red baseline and reported `5 failed` for the same reason. Nothing
was wrong with the code. Kill workers before running the suites.

---

## Phase 8 — partitioning `chunk` by tenant (2026-08-13)

Done at 48 rows, deliberately, because the migration rewrites the table.

### The defect

An HNSW index is a navigation graph, not a sorted list, so a query cannot enter
it at one tenant's rows — they are scattered throughout. Postgres therefore
either uses the index and applies the tenant predicate to whatever it returns,
or ignores the index and brute-forces. The first is dangerous:
`hnsw.ef_search` (default 40) bounds the candidates examined, so a tenant owning
a small fraction of a large index gets almost none of its own rows back. **Ask
for 5 sources, receive 1, with no error.** The chat then answers from thin
context and reads as a mediocre answer rather than a broken retrieval path.

Same shape as the edgeless graph: degraded retrieval indistinguishable from
working retrieval. Invisible at 48 rows because the planner sequentially scans;
it appears exactly when the corpus outgrows eyeballing.

### HASH(16), not LIST

LIST — one partition per tenant — removes the post-filter entirely, but makes
tenant creation a DDL operation and so a privileged schema change. With tenant
cardinality unknown that is a large operational bet. HASH(16) takes the
reduction without it, and `hnsw.iterative_scan = 'strict_order'` (set on the
database) covers the residual mixing by making the index resume scanning instead
of returning short. Convertible to LIST later.

`strict_order` over `relaxed_order`: scores are shown to users and recorded as
evidence, so exact distance ordering is worth the cost.

### Two traps

**The primary key must contain the partition key** — `(id)` became
`(id, tenant_id)`. The unique constraint already led with `tenant_id`.

**RLS is not inherited by partitions.** A policy on the parent governs rows
reached *through* the parent; `SELECT * FROM chunk_p3` is subject only to
`chunk_p3`'s own policies, and `platform_app` holds SELECT on it via default
privileges. Every partition therefore gets its own ENABLE + FORCE + policy.
Without that, this migration would have opened a direct-access hole in the exact
control the table exists to enforce. Verified as `platform_app`: with no tenant
context, both `chunk` and `chunk_p8` return 0 rows.

### The property suite had to be taught about partitions

A partitioned parent is `relkind = 'p'`, not `'r'`. Two tests filtered to `'r'`,
so after the migration they silently **dropped `chunk` from the check** while
admitting its sixteen partitions as unclassified tables. Both now scan
`('r','p')` and collapse partitions onto their parent for the declared-list
comparison, while still asserting each partition is individually protected — a
strictly stronger check than before, covering 17 relations rather than 1.

Confirmed it still bites: `ALTER TABLE chunk_p8 NO FORCE ROW LEVEL SECURITY`
fails the suite naming `chunk_p8`.

### Verified

* 48 rows and all 48 embeddings preserved; `kg-demo` 40, `maintenance` 8.
* 17 relations ENABLE+FORCE with 17 policies; 17 HNSW indexes (parent + 16).
* Plan shows pruning to a single partition, RLS pushed into the index condition.
* Graph query unchanged: 52 nodes, 2 edges, `edgeless: false`, 5 sources.
* Cross-tenant still 0 sources.
* `89 passed`; `make mutate` → all 23 controls load-bearing.

---

## Phase 9 — document lifecycle: replace, delete, rebuild (2026-08-13)

Closes three gaps that looked separate and were one: a document could not be
changed, `build_version` was declared but never wired, and onboarding artifacts
had no invalidation edge back to the corpus they were derived from.

### What was broken

Upload deduplicated on `content_sha256`, so an edited file had a different hash
and became a **new** document. The old one stayed indexed, stayed retrievable,
and could win a similarity match and be cited. The only replace path was
delete-then-upload, by hand, in that order.

`chunk.build_version` existed for exactly this. Every writer used the default
`1`, no reader filtered on it, and `VectorIndex.search(build_version=…)` had no
implementation — a declared mechanism with no behaviour behind it, the same
shape as the Azure build's tool-approval flag routing to a disabled gate.

### The model

Identity is now `(tenant, collection, filename)`; the content hash is the
*version* of that document. `collection_build` names which build a collection
serves. Writes go to N+1 while reads stay on N, and promotion is one
transaction, so a rebuild that dies half-written leaves the previous corpus
serving.

Rebuilds **copy forward**: unchanged chunks are `INSERT … SELECT` with a new
`build_version`, no re-embedding and no cost.

### Findings

**The seeders modelled documents wrongly.** 49 document rows for 48 chunks — one
row per *chunk*, so a two-chunk file became two rows sharing a filename.
Content-hash identity hid it; filename identity broke on it immediately. The
migration repairs it by repointing chunks at a surviving row rather than
superseding the duplicates, because superseding would leave live chunks owned by
a superseded document and the next rebuild would silently drop that content.
`seed_chat.py` now writes one document per file, and `ordinal` — previously
hardcoded `0` — is the chunk's position within it.

**Supersede before insert, not after.** The unique index is partial on
`superseded_at IS NULL` and is checked per statement, so inserting first leaves
two current rows for one filename and is rejected. The first version had this
backwards and the code comment asserted the opposite of what happened.

**Five read sites, not three.** `load_documents` (which feeds the knowledge
graph and BM25) and the eval gate's retriever both read chunks. Missing
`load_documents` would have been worse than missing a ranked path: the graph
would build from every build at once and entity counts would silently inflate.

**A self-referential assertion is a vacuous one.** The first version of
`test_the_reaper_never_deletes_the_live_build` asserted
`remaining <= MAX_COEXISTING_BUILDS` — reading the constant it was guarding.
Raising the constant to 99 kept the test green while reopening the recall
problem. It now asserts against a literal `2` *and* that the constant is `<= 2`.
Verified by mutation: 4 of 4 mutations now fail the suite, where the first
version caught 3 of 4.

### Known limit, not worked around

`POST /api/documents` validates and hashes uploaded bytes but does not retain
them — there is no object-store adapter, only the port — and it does not produce
chunks. So a document whose *content* changed cannot be re-chunked: there is
nothing to re-read. A replaced document drops its old chunks and contributes
none, and the run reports `documents_without_chunks` rather than pretending the
corpus is complete. Closing it needs retained bytes or an ingest path that
chunks on upload.

### Verified live

Upload → re-upload (`unchanged: true`, no new run) → replace (version chain
linked) → delete (404 on repeat) → four rebuilds. Throughout: 40 chunks copied
forward each time, `edges: 2`, `edgeless: false`, 5 sources, identical
retrieval. Coexisting builds stayed at 2 — the reaper enforced the bound.
Onboarding drift detection reports `current`, `drifted` and `unknown` correctly,
the last for sessions drafted before fingerprints were recorded.

`95 passed`; `make mutate` → all 23 controls load-bearing.

**Test-environment note:** one `test_a_publish_failure_leaves_the_row_for_retry`
failure was leftover outbox rows from manual API testing — `outbox.drain()` is
relay-scoped and cross-tenant, so it counted them. Green twice after cleanup.

---

## Phase 10 — object storage and ingest (2026-08-14)

Closes the gap Phase 9 named as the most important remaining one: uploaded bytes
were validated, hashed and thrown away, and nothing outside a seed script could
manufacture a chunk. The lifecycle machinery was protecting an empty envelope.

### What was actually missing

Not a half-built adapter — nothing at all. `ports/object_store.py` defined the
Protocol and exported it; grep found **zero** references anywhere else in the
tree. Meanwhile the substrate had been running MinIO since day one, `boto3` was
already a declared dependency, and `settings.py` already carried
`s3_endpoint_url`, `s3_bucket` and both keys. `list_buckets()` returned `[]`:
nothing had ever been written.

`documents.py` computed `storage_key = f"{slug}/{collection}/{sha}{suffix}"`,
wrote it to the row, and dropped the content. A column pointing at an object that
was never created — the same shape as `build_version` before Phase 9, and as the
Azure build's tool-approval flag routing to a disabled gate.

### The key is derived, then checked again

`key_for` builds `t/<tenant_id>/<parts…>`; every operation re-derives the
caller's prefix and refuses anything outside it. The derivation alone is a
convention — nothing stops a caller passing a `storage_key` it read out of a row
written by different code. The re-check is the control, and the mutation
`object-key-scope-unchecked` proves it: with the check disabled every happy path
stays green, because `key_for` still produces correct keys.

**Tenant id, not slug.** A slug is a display name, and renaming a tenant frees it
for the next one, which would inherit the objects. That is a tenancy bug wearing
a naming bug's clothes.

Refusal is `NotFoundError`, never a separate "forbidden". The key embeds a
content hash, so confirming one exists confirms another tenant holds that exact
file.

### Conditional writes are real here

Probed the pinned MinIO release before designing around it: `IfNoneMatch="*"`
over an existing key returns **412 PreconditionFailed** and the original bytes
survive. So `put(if_absent=True)` is an atomic primitive rather than a
`head`-then-`put` that looks identical in the happy path and loses the race it
exists to handle. The test asserts on the *bytes*, not just the exception — an
adapter that raised after overwriting would pass an exception-only check while
corrupting exactly what the flag protects.

### Bytes before row, deliberately

The two orders fail differently and only one is recoverable. An object with no
row is garbage that costs disk and can be reaped. A row with no object is a
document the platform lists as current, reports as indexed, and can never chunk
— which is precisely the state the endpoint was already in. The key is
content-addressed, so the write is idempotent and a retry after a crash is free.

An object store that is down now yields **503 and no row**, rather than a 201 for
a document whose content was never retained.

### `.pdf`, `.docx` and `.xlsx` were removed from the allowed list

They were accepted, stored and unchunkable. `ALLOWED_SUFFIXES` is now literally
`chunking.SUPPORTED_SUFFIXES`, so the two cannot drift, and the property test
compares against a **literal** `{".txt", ".md", ".csv", ".html"}` as well as the
constant — asserting only against the constant it guards would let a later
widening move the goalposts, the mistake made in Phase 9's reaper test.

Binary formats return when there is an extractor for them. Accepting a file the
platform can never index is not a smaller version of supporting it.

### Chunking is ours, not the engine's

`doc_pipeline.ingestion` has a structure-aware chunker, and its `router.py`
imports `PDFParser` at module scope, which imports PyMuPDF. Reaching for it
pulls the parser stack into the API process to split markdown, from a tree this
project treats as read-only. `platform_core/corpus/chunking.py` is ~350 lines:
heading-aware sections, token windows via tiktoken (already a dependency),
60-token overlap, runt tails merged backwards, HTML `<h1>`–`<h6>` rendered as
markdown headings so section detection has one rule rather than one per format.

`canonical_id` stays `c_<sha1:16>` of the chunk text — the same namespace the
seed scripts use. Verified: prepending a section shifts every ordinal and
changes no id of a chunk whose text is unchanged.

### Embedding sits between two transactions, not inside one

Copy forward in one session, ingest outside any, count in a second. Holding a
Postgres transaction open across an object-store fetch and then the embedding API
is the connection-exhaustion failure `correctness/side_effects.py` describes. The
cost is that a crash mid-ingest leaves a partial build — which is what
`builds.fail` exists for and why the build is written beside the live one.

`BudgetExceededError` propagates rather than being caught. Keeping the chunks
already embedded and promoting what there is would produce a corpus that
silently lost documents and answered confidently from the remainder.

The ingest context is `task="ingest"`, so an unreadable ledger fails **closed**
per the Phase 2 policy. `task="reindex"` would also have failed closed — unknown
tasks do — but relying on that leaves the classification to a default.

### One unreadable document must not fail a collection

Every document written before this phase has a `storage_key` pointing at nothing.
Those are skipped by reason (`bytes_missing`, `no_storage_key`,
`unsupported_format`, `no_text`), counted in `skipped_documents`, logged, and
still counted in `documents_without_chunks`. A `TransientError` from the store is
deliberately *not* a skip: a briefly-down store would otherwise produce a smaller
corpus and promote it.

### Verified live

`make e2e-ingest` against real MinIO and the real embedding API, green on the
first run: upload → `storage_key` tenant-derived → bytes read back byte-identical
→ re-upload is a no-op → build 1 ingests 2 chunks and copies 0 → replace →
build 2 re-chunks (3 chunks, `315 Nm` served, `300 Nm` gone, new `M20` section
picked up) → unchanged rebuild embeds **0** and copies 3.

`93 passed` (13 new). `make chaos` 15 passed. `make load` 4 passed.
`make mutate` → **26 of 26** controls load-bearing, including all three new ones.

### The leftover-outbox failure recurred

`test_a_publish_failure_leaves_the_row_for_retry` failed on the first full run.
Same cause as Phase 9: three unpublished `demo-acme` reindex rows dated
2026-08-13 16:55, left by manual API testing, which `outbox.drain()` counts
because it is relay-scoped and cross-tenant. The run drained them; green on the
next and every subsequent run. **Not** caused by this phase — but it has now cost
an investigation twice, which makes it a real gap in test isolation rather than a
quirk to remember.

### Not done

* **Objects are never reclaimed.** A superseded document keeps its bytes, on
  purpose — reverting to the previous build has to work without a re-upload, and
  that build's chunks are only reproducible while their content exists. There is
  no purge path yet, so storage grows monotonically with every replacement.
* **`.pdf` / `.docx` / `.xlsx` are refused**, where before they were accepted and
  quietly useless. Honest, and still a capability the platform does not have.
* **A rebuild still copies the whole collection** for a one-file change. Ingest
  now only embeds what is new, so the expensive half is fixed; the row copying
  is not.
* **Per-tenant bucket policies were not added.** Isolation is derived keys plus
  a re-checked prefix, backed by property tests and a mutation. The port's own
  docstring argues a policy is a stronger control than a convention, and that
  remains true — the check is application-level, so a caller that bypasses the
  adapter bypasses the boundary.
* **`make init-store` is a separate step** from `make migrate`. Alembic owns
  Postgres and nothing else; a migration that reaches into another service can
  fail for reasons unrelated to the schema with no way to roll that half back.

### Trap · a long-running `--reload` server was serving stale code

Verifying through the live API on :8100 — a `make api` process up for 46 hours —
returned **201 for a `.pdf`** and wrote a `demo-acme/maintenance/<sha>.pdf`
storage key in the old interpolated format, with no object behind it. The tree
was correct; the process was not. uvicorn's `--reload` had stopped picking up
changes at some point over those two days.

Worth knowing because the failure is silent and points the wrong way: every
symptom said "the new code does not work", and the fix is to restart the server.
Verify against a freshly started process, or against the code directly, before
concluding anything about a long-lived dev server.

The two rows and the run/outbox rows that probe created in `demo-acme` were
deleted. One detail confirmed a control on the way out: both uploads carried the
same content hash, so the second `enqueue_run` deduplicated on
`reindex:maintenance:<sha>` and only one outbox row existed to remove.

---

## Phase 11 — a new domain, end to end (2026-08-14)

Question: with content retention working, can an operator take a domain the
platform has never seen from files on disk to a chat answer with a knowledge
graph behind it? Answer: **the flow works; the taxonomy it produces does not.**

### The console could not reach the last step

`web/lib/api.ts` hardcoded `schema_domain: payload.schema_domain || "manufacturing"`
and `app/page.tsx` never passed one. `build_graph` resolves published onboarding
artifacts *by domain name*, so every console graph query asked for
`manufacturing` — and the database showed `manufacturing` sitting at
`draft_ready` with `relations_available=false`, while the only **published**
domain was `kgdemo`.

So the console's graph mode had never once used the artifacts that work. It built
an edgeless graph every time, answered plausibly, and reported no error. The
Phase 7 verification of `edges: 2, edgeless: false` was real — it went through a
script with `schema_domain=kgdemo`, not through the UI.

Fixed: the chat page now fetches `GET /api/onboard/domains`, offers only
**published** domains, prefers one whose `relations_available` is true, labels
those that cannot traverse `— no edges`, and shows a warning chip when nothing is
published at all. The picker only appears in graph mode, because dense retrieval
has no schema dependency and offering the control there would imply otherwise.

`DEFAULT_SCHEMA_DOMAIN` is now an exported, documented constant rather than an
inline `||` fallback — it was the fallback silently winning that caused this.

### `scripts/e2e_domain.py` — the whole flow, live

Generates a 33-file, 29KB corpus for a `datacenter` domain built so relations
recur, then: upload → reindex ingests → onboarding drafts → the reviewer approves
(and the drafter is refused first) → publish → chat in graph mode. Every stage
passed on the first run.

**Chunk count, not byte count, is the gate.** 29KB produced **96 chunks**, well
above the ~30 needed for edge-type synthesis, because the corpus is
heading-dense and the chunker splits on headings. An earlier estimate of
"50–100KB for 40 chunks" assumed unstructured prose and was wrong by more than
2×. The honest rule is: check `chunk_count` after the build, not the file sizes
before it.

### The finding: `relations_available: true` is not enough

The draft reported 19 predicates, 88 instances, 87 cache files,
`relations_available: true`. The graph built with **84 nodes and 1 edge**.

Every intermediate stage looked healthy, so the loss had to be traced:

* 430 relations in the extraction cache; 307 had a mapped predicate *and* both
  endpoints resolvable in the instance table — so the artifacts were fine.
* `CachedRelationExtractor` was constructed and hitting the cache (verified: the
  computed `_cache_key` for sampled chunks matched files on disk).
* `KnowledgeGraph._rejected` gave the answer: **295 of 306 candidate edges
  rejected as `edge_endpoint_type_mismatch`**, plus 183 `entity_type_unknown`.

The drafted schema declares exactly **three** entity types — `Gauge`,
`InfrastructureComponent`, `Sensor` — for a corpus containing racks, PDUs, CRAC
units, switches, alarms, procedures and parts. 42 of 84 nodes fall back to
`raw:<free-form>` types. `EdgeType.accepts(src, tgt)` is
`src in self.source and tgt in self.target`, and every drafted edge type
constrains both endpoints to declared types — so every relation touching a `raw:`
type is discarded.

This is the failure **one step past** the Azure build's silent
`relations_available=false`. There, the artifacts were missing. Here they are
present, complete, and internally consistent, and the graph still cannot
traverse, because nothing checks that the schema's *entity* taxonomy covers what
the instance table actually produced. `relations_available` asks "do the three
artifacts exist"; it does not ask "do they agree".

The script now reports this as a separate section — candidate edges, admitted,
and each rejection reason — and refuses to end on a line that reads like
unqualified success. It is deliberately **not** a flow failure: the pipeline did
its job, and conflating "the machinery works" with "the output is good" is how
the first defect got missed.

### Not done

* **Nothing gates on taxonomy fit.** A draft with a three-type schema over a
  twelve-type corpus is approvable and publishable today, and the operator finds
  out by reading an edge count. The check exists in `diagnose()` in a script; it
  belongs at draft time, on the session's `stats`, where a reviewer would see it
  before approving.
* **Re-drafting is the only remedy** and it costs a full corpus of extraction
  calls. There is no way to widen a taxonomy without paying for it again.
* The diagnosis reads `KnowledgeGraph._rejected`, a private attribute of a class
  in the read-only engine tree. It degrades to an empty dict if that disappears,
  but it is fragile and there is no public accessor.

---

## Phase 12 — the taxonomy is editable, and the eval gate is reachable (2026-08-14)

Both gaps found by comparing the console against `graphrag-azure/web`. The
instruction was to close them without duplicating anything, so the first work was
an audit, and the audit changed the design more than once.

### What the audit found already existed

Almost all of it. `platform_core/gates/` — `datasets.py`, `runner.py`,
`promotion.py` — is complete, tested by ten property tests and a mutation
control, and had **no caller anywhere in the tree** outside
`tests/properties/test_eval_gates.py`. `EVAL_READ`, `EVAL_RUN` and
`RELEASE_PROMOTE` were already defined and already role-mapped. And
`("GET", "/api/eval")` and `("POST", "/api/eval/run")` were **already declared in
the policy table with no handler** — the same forward declaration `/api/query`
carried for several phases.

So nothing here re-implements scoring, dataset versioning or the gate decision.
The work was a workload, a router filling declared paths, and one genuinely
missing function.

### `runner.load` — the one real gap

`promotion.promote` takes an `EvalRun`, not an id, so a caller cannot promote a
run it never looked at. That works when the run was computed moments ago in the
same process, and not at all when a person decides hours later — which is when a
promotion actually happens. Nothing could rebuild a stored run. Added, with
`with_outcomes=False` for the path that only moves a pointer.

### Editing is authoring

The reference lets the reviewer edit the YAML and approve it in one motion,
disclosing it afterwards with a `yaml_edited_by_reviewer` flag. Disclosure after
the fact is a weaker control than refusal before it, so the rule here is the
existing one applied to the same act: whoever last wrote the taxonomy cannot
approve it. Enforced in `store.approve` and by a CHECK constraint
(`onboarding_session_editor_is_not_approver`, migration 0017), exactly as
self-approval already was.

The consequence is deliberate: a reviewer who fixes a taxonomy has made
themselves its author and a different principal must sign it off. Editing takes
`schema:author`, not `schema:approve`.

### A schema edit alone does not fix anything — found by a failing test

The first version let a reviewer rewrite the YAML and stopped there. A test
asserting that declaring the missing entity types drives `instances_unclassified`
to zero **failed**, and it was right to.

`KnowledgeGraph` admits an instance-table entity whose type the schema does not
declare *as a node carrying the literal type* `raw:alarm`, and
`EdgeType.accepts` compares that string against the declared endpoint types. The
types live in the instance table, which the drafter derived under the **old**
schema. Adding `Alarm` to the taxonomy therefore changes nothing: the node is
still `raw:alarm` and every edge touching it is still discarded.

So `edit_schema` retypes the instance table in the same transaction, and the
route refuses a retype target the edited taxonomy does not declare — mapping one
unusable type onto another reads as a fix and is not one. Had the test asserted
only that the YAML was stored, this feature would have shipped looking correct
and changing nothing, which is the exact defect class this codebase exists to
catch.

### `taxonomy_fit` — the signal that was always available and never surfaced

The instance table records `entity_type` **and** the free-form `raw_type` it fell
back from. So "does this taxonomy cover the corpus" is answerable the moment a
draft finishes, with no corpus scan and no LLM call. On the `datacenter` domain:
**74 of 88 instances unclassified (84%)**, with the missing types named and
counted — `rack` 22, `procedure` 10, `alarm` 9, `power distribution unit` 6.

It is now on `GET /api/onboard/sessions/{id}`, so a reviewer sees it before
approving rather than inferring it from an edge count afterwards. The console
opens the taxonomy section automatically when more than a quarter of instances
are uncovered.

`validate_schema_yaml` also reports `unreachable_edge_types` — edge types whose
endpoints are not declared. Worth having and **not** the signal that mattered
here: the drafted `datacenter` schema was internally consistent and still admitted
1 of 306 edges.

### A latent bug the edit exposed

`artifacts_for` resolved a singleton artifact by `kind` alone. With one row per
kind that is correct; with two it returns whichever row came back last, so the
published schema would have depended on a query's ordering. Nothing had a second
row until edits introduced `schema_drafted`. Now resolved by `name = kind`, which
finally uses the `SINGLETON_KINDS` constant that had been defined and unreferenced
since Phase 7.

### Authority split on the eval surface

Three acts, three capabilities. Reading a score is `eval:read`. Producing one is
`eval:run` — an **operator** capability, because making people ask permission to
measure is how measurement stops happening. Moving the baseline **and writing a
dataset version** are both `release:promote`: whoever can rewrite the questions
can make any regression pass, so gating authorship any lower would leave the
promotion capability guarding a door with no wall attached.

The workload evaluates and does **not** promote. A worker promoting its own
candidate is the release equivalent of approving your own schema.

### Verified

Live through `TestClient` against the real middleware: owner writes a dataset
(201), operator is refused (403, `capability=release:promote` in the log), a
`page:3` citation is rejected (400), operator queues a run (202), unknown dataset
404s. Then a real run against the live `maintenance` build — real embeddings,
real pgvector retrieval, `retrieval_recall 1.0`, gate verdict computed, **baseline
not moved**. Test data removed afterwards.

`110 property tests` (17 new), 15 chaos, 4 load, **30 of 30 mutations caught**.
Console builds clean.

### Failure · the mutation harness overwrote a source file

`make mutate` reported all 30 controls caught and then printed
`suite did not return to green after restore: 121 passed, 4 errors`.

`MUTABLE_FILES` is backed up as `{f: backup_dir / f.name}` — keyed on the
**basename**. Adding `workloads/eval/workload.py` to that list put it in the same
backup slot as `workloads/reindex/workload.py`, and the restore wrote the eval
workload's source over the reindex workload's.

Three things about this are worth keeping:

* **The mutation results were still reported as caught.** Every mutation ran and
  the suite went red each time, for the right reason; the damage happened during
  restore, after each verdict was recorded. A harness that verifies controls is
  not itself a verified control.
* **The trailing "did not return to green" line is the only thing that said so**,
  and it was printed mid-stream among thirty verdicts. It was added in Phase 4
  after a timeout left a control disabled — the second time it has been the only
  warning of a corrupted tree.
* It was caught because that line was read, not because anything failed loudly.

Fixed by keying backups on the path relative to the repo root. The reindex
workload was reconstructed and verified by `make e2e-ingest` against real MinIO
and the real embedding API — upload, replace, re-chunk and a copy-forward rebuild
that re-embedded nothing — not only by unit tests.

### Not done

* **No candidate-query gate and no clarifying-questions gate.** The reference
  stops twice before drafting: it proposes questions the domain must answer and
  asks the SME to keep, edit or reject them, then asks clarifying questions. The
  approved queries drive a coverage check *and* are what its eval set is seeded
  from — so their absence is why eval sets here must be authored by hand.
* **No eval-set seeding or reference-answer drafting.** Depends on the above.
* **No LLM judge**, so `answer_pass_rate` is always null and the gate compares
  retrieval recall alone. `runner.run` takes a `judge` callable; nothing supplies
  one.
* **`GET /api/eval` is not paginated** and returns every dataset.
* Retyping maps free-form types onto declared ones by exact string. A corpus
  where the same concept appears as both `rack` and `Rack` needs two entries;
  the console offers each separately rather than guessing they are the same.

---

## Phase 13 — grading the answer, not just the retrieval (2026-08-14)

`answer_pass_rate` had been null since Phase 4: `runner.run` accepted a `judge`
and nothing supplied one, so the gate compared retrieval recall alone. Closing
that needs three roles, and the value is entirely in keeping them apart.

### Three models, two of them constrained

| role | setting | default | must differ from |
|---|---|---|---|
| answerer | `llm_model_cheap` | `gpt-4o-mini` | — (it is the thing measured) |
| annotator | `llm_model_annotator` | `gpt-4.1-mini` | the answerer |
| judge | `llm_model_judge` | `gpt-4.1` | the answerer **and** the annotator |

Enforced in `Settings.check_coherence`, which refuses to start on a collision.
The reference deployment states the reason better than a summary can: *"a judge
that shares a model with `answer` marks its own homework, and nothing about the
resulting numbers looks wrong — they are simply flattering."* It has no symptom,
so it cannot be a comment.

**The reference's model ids were not copied.** It uses `gpt-4-turbo` / `gpt-4o-mini`
because *its* answerer is `gpt-4o`. This platform answers with `gpt-4o-mini`, so
its annotator id would have collided with the model being measured. Both roles
were chosen instead to differ from the answerer and to exist in `MODEL_PRICING` —
an unpriced model is charged at the highest known rate, and every cost number
involving it would then be wrong.

### The annotator reads evidence, never retrieval

This is the property everything rests on. A reference written from what
retrieval returned could only ever contain what retrieval returned, so a
retrieval miss would be **structurally undetectable**. `gates/annotator.py` is
handed the chunks each item cites, read from the live build, and nothing else —
asserted on the prompt itself in `test_the_annotator_is_shown_evidence_and_never_the_retriever`.

It never overwrites an answer whose `answer_source` is `sme_edited` or
`sme_authored`, and `labels.record_drafted` refuses the downgrade independently,
so re-running drafting cannot quietly replace reviewed ground truth.

### Labels live outside the content hash — migration 0018

The subtle one. A dataset is `(name, content_sha256)` over the item dicts, and
`promotion.evaluate` refuses to compare across hashes. Putting `confirmed` or
`answer_source` inside the item would mean **ticking a checkbox mints a new
dataset version and orphans the baseline** — the reviewer punished for reviewing,
on the first click of the workflow this exists to support.

So `eval_item_label` is keyed by `(tenant, dataset_name, item_id)`. Two
consequences worth having: confirming never perturbs the hash, and labels are
keyed by *name* rather than version, so they survive re-versioning — a reviewer
does not re-confirm forty items because one answer was drafted.

`question`, `expected_answer` and `must_cite` stay in the hash. Editing an
expected answer *should* mint a version: it changes the yardstick, and comparing
against a baseline scored on different references is exactly the incomparability
the gate refuses.

### Honest denominators

Three counts that were one before:

* **`judge_unavailable`** — items the judge could not grade are excluded from the
  pass rate, not counted as failures. The reference shipped the opposite: an
  unsupported `response_format` returned a 400, a blanket handler turned it into
  "judge unavailable", and the run reported a quality collapse while nothing was
  wrong with the answers. The judge here degrades `json_schema → json_object →
  none` for the same reason.
* **`items_excluded`** — items a reviewer flagged as having unusable evidence are
  dropped and counted. A set that quietly shrinks stops being comparable.
* **`accepted_unedited`** — confirmed while still `llm_drafted`. Without it,
  "reviewed" and "clicked through" are the same number, and the console says so
  in words rather than leaving it to be inferred.

### `fix_surface`

A failing verdict names a closed enum — `prompts:answer`, `retrieval:top_k`,
`kg:instance_table`, `kg:entity_type`, `corpus:gap`, `eval:expected_answer` —
and the run reports the distribution. Twelve failures all naming `kg:entity_type`
is a different morning's work from twelve naming anything, and that is only
visible as a count.

### Deterministic metrics alongside the judge

`gates/metrics.py`: faithfulness, answer relevancy, context precision, citation
accuracy. Free, reproducible, computed on **every** item where the judge grades a
handful — so a regression has somewhere to appear between two verdicts. Each
averages over its own denominator and reports the count beside it, because a mean
over "items where it applied" and a mean over "all items, counting the rest as
zero" are different numbers and only the first means what a reader assumes.

`context_precision` is documented as a ceiling rather than a grade: `must_cite`
is the *minimal* evidence, not an exhaustive relevance judgement, so one cited
chunk at `top_k=5` can never exceed 0.2 however good retrieval is.

### Failure · faithfulness was measured against the wrong context

The first live run reported **faithfulness 0.32** for answers that were entirely
grounded. The metric was being given the item's `must_cite` evidence, while the
answerer had been given the *retrieved* chunks — so an answer was scored
unfaithful for using other chunks retrieval had legitimately returned. The same
mistake the judge's rubric is careful not to make, one metric lower down.

Fixed to measure against what the answerer was actually shown. The same run then
reported **0.91**. Nothing in the unit tests would have caught it: every number
was real and internally consistent, and only running it against a real corpus
made the value visibly wrong.

### Verified

Live end to end on the demo corpus: 3 blank items → annotator (`gpt-4.1-mini`)
drafted 2 and **refused the third**, whose cited chunk did not answer its
question → new dataset version → one item edited and confirmed, one confirmed
unread → run with `gpt-4o-mini` answering and `gpt-4.1` judging → recall 1.0,
pass rate 1.0 over the two gradeable items, the third excluded as
`judge_unavailable` with `fix_surface: eval:expected_answer`.

Live through `TestClient`: confirming without editing leaves `answer_source` at
`empty` and does **not** change the version; editing promotes it to `sme_edited`
and does; an operator is refused with `capability=release:promote`; an unknown
item 404s.

`121 property tests` (11 new), 15 chaos, 4 load, **35 of 35 mutations caught**,
console builds clean. Test data removed from `demo-acme` after each live run.

### Not done

* **No candidate-query gate**, so eval sets are still authored by hand. The
  reference seeds them from SME-approved onboarding queries, which is why its
  items carry `origin: onboarding_approved` and evidence ids for free.
* **The judge is called once per item with no self-consistency check.** LLM
  judges are known to be lenient and to favour verbose answers; nothing here
  measures that bias, and the deterministic metrics are the only counterweight.
* **`requires_kg_hop` is recorded and not yet reported per-slice.** It is the
  instrument that would make a taxonomy fix provable — score the KG-hop items
  before and after — and the run reports the label count without splitting the
  metrics on it.
* **No cassette coverage for the judge or annotator**, so both suites need a live
  key to exercise end to end.

---

## Phase 14 — eval sets seeded from the corpus (2026-08-14)

The last "authored by hand" caveat in the eval loop. Writing a golden set by hand
meant writing its evidence chunk ids by hand, and an id one character wrong
scores a permanent miss indistinguishable from a real retrieval failure — the
exact defect `build_dataset` rejects non-canonical citations to prevent.

### The hard part was already solved here

`azure_deploy_graphrag/eval_store.py` carries a long apology: its onboarding
drafter chunks the corpus independently of ingestion, so `evidence_chunk_ids` and
the published chunk ids are *"two different namespaces over two different chunk
sets. No derivation reconciles them after the fact — verified by trying every
combination of (source, section_path, ordinal) against the published metadata and
matching none."* It falls back to locating evidence by `source_file` + `page` and
states plainly that this *"does NOT make retrieval recall scoreable"*.

The engine supports doing it properly. `_chunk_identifier` reads
`getattr(c, "chunk_id", "")` first, and says why: without a canonical id
*"`CandidateQuery.evidence_chunk_ids` … live in a private namespace and every
recall / citation metric computed against them reads 0.0."*

**This platform can supply one.** Onboarding reads the corpus through
`PgVectorRetriever.load_documents()`, whose `chunk_id` *is* the canonical
`c_<sha1:16>` from the live build — the same id the retriever returns and the
same one `datasets.build_dataset` validates. One chunking pass, one namespace.

So the fix is a ~15-line adapter (`_chunk_views`) carrying that id into the
engine's existing `propose_candidate_queries`. No sampling logic, no prompt and
no parser was reimplemented.

### Where it runs, and what was deliberately not copied

The reference pauses the orchestrator mid-graph for a human gate and resumes it
via `POST /session/{id}/resume`. That was **not** copied. Local's onboarding is a
single-shot queued workload and `engine.install()` sets `USE_HITL=false`
deliberately; a run that stops, waits days and resumes brings new failure modes
around leases and sweeps for no gain here.

Instead the queries are generated *after* `analyze` completes and captured as a
`candidate_queries` artifact (migration 0019 widens the `ARTIFACT_KINDS` CHECK).
The session already sits at `draft_ready` waiting for a human, and the taxonomy
editor is already on that screen. Generating after the expensive step also means
a failure costs four calls rather than the whole draft — `_propose_queries` never
raises, and the import sits **inside** the guard because an engine that will not
import is precisely the case it promises to survive. It was outside it once.

### Two authorities, again

Curating questions is `schema:author` — writing what a domain must answer is
authoring, exactly as rewriting its taxonomy is. Seeding is `release:promote`,
because it creates the thing the gate measures against, the same authority as any
dataset write.

Nothing is approved by default, and only approved questions seed. Editing sets
`edited` independently of `approved`, the same distinction the eval set draws
between `sme_edited` and `confirmed`: a question a human rewrote and one they
waved through are different evidence that anybody read it.

Expected answers are left blank. They are drafted by the annotator and read by a
person in the Evaluation panel, because a question and its reference answer are
two separate acts of judgement and collapsing them is how an unread set becomes
"ground truth".

### Verified live, with nothing hand-authored

Against the 96-chunk `datacenter` corpus:

* 4 questions proposed, **every citation canonical** — e.g. *"What causes alarm
  ALM-512 on rack RK-08?"* citing `c_cb73675903f45aeb`;
* seeded to a dataset, 4 items, **4 scoreable**, zero LLM cost;
* annotator (`gpt-4.1-mini`) drafted 4 of 4 from the evidence;
* run: **recall 0.5, pass rate 0.75**, judged by `gpt-4.1`, faithfulness 0.75,
  relevancy 0.82.

Recall 0.5 is a genuine finding, not a defect in the plumbing: each question
cites one chunk and retrieval at `top_k=5` returned it for two of four. That is
the first measurement of this domain that means anything, and it came from a
corpus with no hand-written eval set behind it.

`132 property tests` (11 new), 15 chaos, 4 load, **37 of 37 mutations caught**,
console builds. The step is now part of `make e2e-domain`. Test data removed from
`demo-acme`.

### Not done

* **No coverage check before drafting.** The reference gates queries *first* so
  approved questions can drive a "does this taxonomy cover these" report. Here
  they come from the same run, so the equivalent is after the fact: run the
  seeded set and read the `fix_surface` distribution — `kg:entity_type` and
  `kg:instance_table` failures *are* the coverage report, measured rather than
  predicted. Cheaper and arguably better, but it is not the same thing and it
  arrives later.
* **A short-chunk corpus proposes nothing.** `_stratified_sample` drops chunks
  under 300 characters, so the six-chunk demo set yields zero questions. Surfaced
  as a count on the session and stated in the console rather than rendered as an
  empty list, for the same reason `relations_available` is.
* **`entity_hints` are captured and unused.** The generator emits noun phrases it
  thinks are entities; they would feed the taxonomy-fit report directly, and
  nothing reads them yet.
* Questions are generated from the corpus alone. Nothing proposes the questions a
  *user* actually asked — the session table has them, and mining chat history for
  unanswered questions would be a better source than sampling chunks.

---

## Phase 15 — P0/P1 production control baseline (2026-08-16)

This phase re-reviewed the repository as a large-scale production agentic
system. Telemetry, evaluation, recovery, governance and supply-chain controls
were treated as mandatory baseline, not optional enhancements.

### P0 delivered

* **Admission and identity:** default-deny route policy, tenant-bound JWTs,
  principal-state revalidation, browser origin/proxy constraints, restricted
  privileged routes and fail-closed privileged audit admission.
* **Mandatory observability:** OTLP traces, metrics and scrubbed structured logs;
  pseudonymous tenant labels; collector reachability in readiness; startup
  refusal for disabled/insecure production telemetry; 20 Prometheus alerts, a
  Grafana platform dashboard and a checked-in runbook for every alert.
* **Durable agent execution:** Postgres checkpoints, leases and heartbeats,
  transactional outbox, immutable ordered step state, idempotent effect claims,
  reconciliation state, tool receipts and cancellation points.
* **Tool safety:** bounded registry, capability checks, immutable exact-argument
  approvals with maker/checker separation, timeouts, output limits and
  fail-closed treatment of unknown tools.

### P1 delivered

* **Capacity and cost:** fair tenant queue ordering, distributed rate limits,
  per-tenant concurrency, pre-dispatch token reservations, hard daily/monthly
  caps, complete attribution and conservative pricing for unknown models.
* **Resilience:** bounded retry with jitter and explicit retry classes,
  cooperative cancellation, crash recovery at every effect boundary, durable
  scheduler/outbox sweeps and role-specific health checks.
* **Governance:** tamper-evident tenant audit chains with retention anchors,
  bounded retention and erasure workflows, mandatory continuous-evaluation
  schedules, independent judge/annotator settings, versioned datasets and
  promotion/canary rollback gates.
* **Delivery:** API, run worker, relay, maintenance, scheduler and migrator are
  separate authorities; final images are non-root and multi-stage; Python/npm
  locks, dependency and image vulnerability gates, pinned actions/base images,
  digest release manifests, SBOM/provenance and hardened Kubernetes base
  resources are enforced in CI/release workflows.

### Failures found during the production-shaped verification

* **Compose had broader credentials than the process roles claimed.** Secrets
  were split by runtime authority and a static deployment-policy test now
  rejects owner/relay/JWT/model credentials in the wrong process.
* **Readiness disclosed resolved configuration.** A Pydantic Postgres DSN did not
  pass the old string-key redactor, so the anonymous `/health/ready` response
  contained a database password. Readiness now returns control state only, never
  resolved configuration or detailed configuration errors; a property test and
  mutation make the boundary load-bearing.
* **The local virtual environment pointed at another checkout.** Its `pytest`
  shebang used a sibling tree and masked an undeclared NumPy runtime dependency.
  The environment was recreated from the frozen lock, NumPy and PyYAML were made
  explicit runtime dependencies, and no stale entry-point shebang remains.
* **GraphRAG schema validation imported the entire external runtime.** Pure YAML
  validation unnecessarily required document/vector/network packages. Import
  preparation is now separate from runtime installation, so schema validation
  loads only the schema surface while preserving the canonical engine boundary.
* **The web runtime was based on end-of-life Node 20 and retained package
  managers.** It now uses pinned Node 24 LTS and strips npm/Corepack/Yarn from
  the final image. Both final images scan with zero fixable high/critical
  findings.
* **Runtime host ports were hard-coded.** `API_PORT`, `WEB_PORT` and bind-address
  overrides now support production-shaped local verification without weakening
  browser origin policy.
* **A release tag could bypass the slow control evidence.** The publish workflow
  ran unit/property checks but not crash recovery, load/contention or mutation
  checks. All three now gate a tag. The CI attestation check also used to prove
  only that each OCI archive was nonempty; it now walks the OCI descriptor graph,
  requires attached SPDX and SLSA v1 predicates and verifies that their subject
  is an image manifest in the same archive.
* **The mutation verifier could be interrupted mid-mutation.** It now backs up
  every touched path by its repository-relative name, refuses visible mutation
  markers, restores the complete set on failures and bounds every child suite.
  A later verifier process was allowed to finish before a new run, avoiding two
  processes mutating the same controls concurrently.

The live-looking OpenAI credential found in the former `.env` file was removed
and `.env` is ignored. Deletion is not revocation: the credential must be rotated
because shell history, backups or prior copies may still contain it.

### Verification evidence

* `166` unit/property checks, `15` chaos checks and `4` load checks pass in the
  recreated environment. The combined property/chaos baseline is `171 passed`.
* **38 of 38 mutations are caught**, followed by a green restore run. These cover
  RLS, route admission, readiness secrecy, idempotency, lease expiry, retry,
  budgets, attribution, audit integrity, promotion/canary gates, tool/storage
  isolation and evaluation provenance.
* Ruff, bytecode compilation, the frozen-lock check, migration-head check,
  Compose rendering, Kubernetes policy and all Prometheus rule checks pass.
* `pip-audit --strict` and `npm audit` report no known dependency
  vulnerabilities. Trivy reports no fixable HIGH/CRITICAL findings in either
  final image.
* The runtime and web artifacts were generated with SPDX SBOM and SLSA v1
  provenance attestations. Their final users are `10001:10001` and `node`.
* A local Postgres backup was checksummed and restored into an isolated database;
  migration `0023` and three tenants were verified, then the drill database was
  removed. This is database restore evidence, not a claim of complete production
  recovery.
* The full role-separated Compose stack became healthy on alternate host ports;
  the public readiness response was inspected and contained dependency/control
  status without configuration or credentials.

### Production launch is still environment-gated

This closes the application and repository P0/P1 baseline. It does **not** make
an arbitrary environment production-ready. Launch still requires an overlay
with digest-pinned released images, real secret-manager/workload-identity
bindings, private endpoints and approved egress; managed multi-zone data stores
with PostgreSQL PITR plus versioned/replicated object recovery; real alert and
on-call routing; measured capacity/quota ceilings; and successful full recovery,
credential-rotation, erasure, rollback and canary drills. The mandatory evidence
contract is `docs/production-readiness.md`, and release policy intentionally
rejects the base manifests until an environment supplies it.

## Phase 16 — the Phase 15 controls are made load-bearing (2026-08-16)

Phase 15 shipped the largest control surface in the tree — governed tools,
admission control, governance sweeps — with property tests but with nothing
showing those tests would fail if the control were removed. `MUTABLE_FILES`
omitted `agent/tools.py`, `security/rate_limit.py`, `correctness/outbox.py` and
the governance modules entirely, so by this repository's own standard those
controls were unverified. This phase closes that.

Nine mutations were added, taking the set from 38 to **47**:

| Mutation | Control it removes |
| --- | --- |
| `approval-is-reusable` | `consumed_at` — one signature buys one write |
| `approval-ignores-argument-hash` | an approval binds to the call, not the tool |
| `replay-ignores-tool-identity` | replay may not return another call's result |
| `write-tools-may-skip-approval` | the gate cannot be forgotten at registration |
| `rate-limit-admits-one-over-quota` | the admission comparison itself |
| `rate-limit-fallback-never-refuses` | a Redis outage is not an unmetered surface |
| `failed-publish-marked-delivered` | the `continue` *is* the outbox retry |
| `continuous-eval-schedule-is-optional` | every dataset version gets a schedule |
| `retention-deletes-audit-without-anchoring` | the chain head survives deletion |

Two suites are now addressable from the harness: `UNIT` for controls that are
pure functions of settings or a spec, so registration-time and admission
invariants no longer drag a database-backed suite behind every mutation.

### The gap this found

`approval-is-reusable` was **not caught**. Removing `consumed_at IS NULL` from
the approval-consumption UPDATE left all 157 tests green, which means the
single-use property of a tool approval was never tested at all.

`test_write_tool_approval_is_exact_single_use_and_idempotent` looked like it
covered this and did not. Both of its reuse attempts are refused for a reason
other than the one being claimed:

* replaying the **same** idempotency key short-circuits on the execution
  receipt and never reaches the approval;
* the reuse attempt with **different** arguments is refused by the argument
  hash, whether or not the approval had already been spent.

Nothing ever invoked an already-consumed approval with the arguments it was
granted for — the one path where `consumed_at` is the only thing between a
single reviewer signature and a second external write.

Two property tests were added:

* `test_a_consumed_approval_cannot_authorise_a_second_execution` — same tool,
  same arguments, fresh idempotency key.
* `test_an_approval_authorises_only_the_arguments_it_named` — a live,
  *unconsumed* approval must refuse substituted arguments, must not be burned
  by the refusal, and must still work for what it named. Without the last
  clause the assertions would also pass if approvals never worked at all.

### A mutation deliberately not added

Freezing `continuous_eval_policy.next_run_at` to prove the scheduler is
idempotent would **not** have been caught, and correctly so: the schedule key is
derived from `next_run_at`, so a frozen timestamp produces the same key and
`ON CONFLICT DO NOTHING` still collapses the duplicate. That is defence in
depth, not a test gap — but it does mean "one run per due window" currently
rests on the unique index alone, and a change to how the key is derived would
remove the protection silently.

### Verification

* Baseline `173 passed` (properties + chaos), up from 171 — the two new tests.
* **47 of 47 mutations caught**, followed by a green restore run and a tree with
  zero leftover mutation markers.
* Ruff clean.

### Operational note

The first two harness runs aborted on a red baseline:
`test_relay_batch_interleaves_tenants` and
`test_a_publish_failure_leaves_the_row_for_retry` both failed against leftover
undrained outbox rows from a worker left running during manual console testing.
`outbox.drain()` is cross-tenant, so the failing run cleared the rows itself and
the re-run was green. Worth recording because the known symptom was *one*
outbox test failing; it can present as two, and the second is a relay-fairness
test in a different file.

### Correction — the dependency audit was auditing the wrong thing (2026-08-16)

The first CI run on `main` failed at `pip-audit --strict`. It reported no
vulnerability. It reported this:

```
local-platform: Dependency not found on PyPI and could not be audited (0.1.0)
```

`pip-audit` was auditing the installed *environment*, which since the Phase 15
packaging fix includes this project itself. `local-platform` is not published to
PyPI, the lookup fails, and `--strict` promotes "could not be audited" to a build
failure. A red build naming a package that has no advisories to find.

This is a **correction to a Phase 15 claim**. That phase recorded that
`pip-audit --strict` reported no known vulnerabilities, and it did at the time —
then the same phase made the project installable, which put it into the audited
set, and the check was never re-run afterwards. The claim was true when written
and false when committed. The first honest CI run is exactly what surfaced it.

`--skip-editable` is not the fix: `--strict` treats a skipped distribution as an
error too, so it fails with `distribution marked as editable` instead.

`scripts/audit_python_dependencies.sh` now exports the lock with
`--no-emit-project` and audits that. This is better than a suppression: it audits
the 117 third-party dependencies that actually ship, independently of how any
particular virtual environment was assembled — the same reason the lock is the
authority everywhere else here. Result: **no known vulnerabilities**, so nothing
was being hidden.

`check_deployment_policy` now refuses any `pip-audit` invocation in the Makefile
or either workflow that does not pass `-r`, so simplifying the script away
reinstates a failing policy gate rather than a silently false audit. Verified by
reinstating the old form and confirming the gate fires.
