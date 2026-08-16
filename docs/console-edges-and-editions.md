# Console, Edges, and Editions

An engineering record of three problems solved in `local-platform` on 2026-08-13,
written for someone who needs to understand *why* each decision was made, not
just what changed.

The platform already had a working API: tenant isolation enforced by row-level
security, a transactional outbox, budget ceilings, capability-based
authorisation, 74 property tests and a mutation harness. What it did not have
was a way for a person to use it, a knowledge graph with any edges in it, or any
way to change a document once uploaded.

---

## Where we started

Three gaps, each invisible in a different way.

**No user interface at all.** Zero `.tsx`, `.ts` or `package.json` files anywhere
in the tree. Notes described "a chat surface at `POST /api/query`" — which is an
*endpoint*, not a page, and easy to misread as a shipped feature.

**Every knowledge graph had zero edges.** Not an error — a graph with 8 nodes and
0 edges answers questions exactly like a populated one, with no warning and worse
results. Entity matching worked; neighbour traversal never ran.

**A document could not be changed.** Uploads deduplicated on content hash, so an
edited file was simply a *different* document. The old version stayed indexed,
stayed retrievable, and could be cited as the answer.

---

## Part 1 — The console

### What was ported, and what was not

The reference app in `graphrag-azure/web/` is ~4,265 lines of Next.js. Its client
calls about 30 endpoints. Only **two** matched by name — and not the two expected.

The auth and health routers carry **no `/api` prefix**. The real paths are
`/auth/login`, `/auth/me` and `/health`. An early assumption of
`/api/auth/login` would have produced a 404 that reads like a missing feature.

| Decision | What happened |
|---|---|
| Chat page | Rewritten, not ported. The reference UI has no concept of `mode`, per-source `signals`, `edgeless`, `cache_hit`, or per-turn cost — all of which this API returns |
| Admin page | Largely new. This platform's admin surface (approvals, capability grants, budget caps) differs from the reference |
| Voice hooks | **Dropped.** No `/api/voice/*` exists here, and a microphone button that always fails is worse than none |

### Requests are same-origin on purpose

The browser talks to the Next server, which rewrites `/api/*`, `/auth/*` and
`/health` through to the API. This is why the FastAPI app still has **no CORS
middleware**: same-origin requests raise no preflight, so the default-deny
surface needs no `OPTIONS` exemption. Pointing the browser directly at port 8100
would have required punching that hole.

Do not "fix" the missing CORS configuration. Its absence is the design.

### A vulnerable dependency, caught on install

`npm` flagged `next@14.2.5`. One advisory —
[GHSA-p9j2-gv94-2wf4](https://github.com/advisories/GHSA-p9j2-gv94-2wf4),
server-side request forgery **via rewrites** — landed directly on the proxy that
had just been written. Bumped to `14.2.35`, `postcss` to `8.5.26`.

Two advisories remain and are **accepted, not fixed**: both live inside Next
itself and only clear by moving to Next 16, which is breaking. Neither is
reachable here — no `next/image` remote patterns are configured, and the bundled
postcss only processes our own CSS.

### One new endpoint

`GET /auth/me` was added so a persisted token can be validated on page load.
It is **guarded**, not public. A public variant would need to decode the bearer
token itself, creating a second authentication path beside the middleware's —
and the softer of two auth paths is the one that eventually gets used.

It returns the caller's capabilities so the console can hide controls that would
only ever 403. That is presentation only; the server checks every request
regardless.

---

## Part 2 — Making edges real

### The discovery

The artifacts that give a graph its edges are **not** produced by the onboarding
API. They are a side effect of the engine's `_step_bootstrap_artifacts`, which
writes to paths derived from `Path(__file__).parents[N]` — inside the engine
tree, which is read-only here, and not tenant-aware.

Three artifacts are required together:

- `instance_table` — the learned entities
- `predicate_map` — the learned relations
- `extraction_cache` — per-chunk extraction output

Missing any one, `CachedRelationExtractor` is never constructed and the graph
builds with zero edges.

### Five seams, not three

Each is reached through a *deferred import*, so rebinding the module attribute is
observed by the engine without touching a line of its source.

| # | Seam | Why it needed handling |
|---|---|---|
| 1 | `InstanceTable._path_for` | instance table reads |
| 2 | `extraction._CACHE_DIR` | a module constant, not a function |
| 3 | `relation_bootstrap.load_predicate_map` | the caller computes the path **inline**, so the loader itself must ignore its argument |
| 4 | `save_to` on both `bootstrap_*_from_llm_cache` | artifact **writes** |
| 5 | `kg.schema.load_default_schema` | **missed on the first pass** |

The fifth is the instructive one. The engine resolves schemas through
`config.SCHEMA_PATHS`, a dict snapshotted from its own tree at import time. A
newly onboarded domain is not in it, so an approved taxonomy failed with
`unknown domain 'kgdemo'` — unusable by the very thing it was drafted for.

The redirect target is **thread-local**. FastAPI runs sync handlers in a
threadpool, so a module-global root would let one tenant's build read another's
instance table — silently, producing a *plausible* graph, which is the hardest
kind of wrong to notice.

### The unmetered side door

`engine.install()` routed `encode()` (embeddings) through the platform's
instrumented client and nothing else. The onboarding orchestrator makes hundreds
of **chat** calls — one per chunk for extraction, then eight synthesis steps —
straight to OpenAI through `core.llm_client.call_llm`.

Left alone, the single most expensive path in the platform was the one path a
tenant budget ceiling could not stop.

`call_llm` is the right seam because it is the only one: `llm_router.call`
delegates to it, and all 26 engine call sites reach it. After patching, one draft
over 40 chunks measured:

| Model | Calls | Cost |
|---|---|---|
| gpt-4o (synthesis) | 8 | $0.0411 |
| gpt-4o-mini (extraction) | 40 | $0.0011 |
| text-embedding-3-small | 9 | $0.000004 |

Every row carries `workload=onboarding`. Before the patch, none of it existed.

### Corpus size decides whether you get edges

Two drafts were run live.

**6-chunk demo corpus** → 1 entity type, **0 edge types**, predicate map with
**0 entries**, `relations_available=false`.

Not a bug. Edge-type synthesis only promotes a relation that *recurs* across
chunks; a corpus where every relation appears once is indistinguishable from
noise, and treating it otherwise fills a taxonomy with spurious edges.

**40-chunk corpus**, built for recurrence on purpose → 5 entity types,
**8 edge types**, 36 instances, **11 predicates**, `relations_available=true`.

After approve and publish:

```
before:  edges: 0,  edgeless: true,   graph_hits: 0,   entities_matched: []
after:   edges: 2,  edgeless: false,  graph_hits: 10,  entities_matched: ["F-207", "fault F-207"]
```

That is traversal GraphRAG, where before there was only entity-match GraphRAG.

### Authority is split three ways

The reference build gates all of onboarding on a single `admin` role, then needs
a side-channel reviewer table the moment one action needs finer authority.

| Action | Capability | Why separate |
|---|---|---|
| Draft | `schema:author` | spends real budget, writes a proposal |
| Approve / publish | `schema:approve` | a different act by a different person |
| Read a draft | `schema:read` | a reviewer must be able to *look* before deciding |

Maker-cannot-be-checker is enforced three times: a capability check, a
conditional `UPDATE`, and a `CHECK` constraint in the migration. Verified live —
the owner who drafted a schema is refused approval of it.

---

## Part 3 — Scaling the vector search

### The defect, in plain terms

An HNSW index is a *navigation graph*, not a sorted list. A query enters at one
point and hops toward its target. One tenant's rows are scattered throughout, so
there is no way to enter the graph "only at their data".

Postgres therefore had two options: use the index and apply the tenant filter to
whatever came back, or ignore the index and brute-force. The first is dangerous.
`hnsw.ef_search` (default 40) bounds how many candidates are examined — so a
tenant owning a small fraction of a large index gets almost none of its own rows
back.

**Ask for 5 sources, receive 1, with no error.** The chat answers from thin
context and reads as a mediocre answer, not a broken retrieval path.

Invisible at 48 rows, because the planner simply scans. It appears at exactly the
point the corpus outgrows eyeballing.

### HASH(16), not LIST

LIST — one partition per tenant — removes the post-filter entirely, but makes
creating a tenant a DDL operation, and therefore a privileged schema change. With
tenant cardinality unknown that is a large operational bet.

HASH(16) takes the reduction without the bet, and
`hnsw.iterative_scan = 'strict_order'` (set on the database) covers the residual
mixing by making the index *resume* scanning rather than returning short.
Convertible to LIST later.

`strict_order` over `relaxed_order` because scores are shown to users and
recorded as evidence, so exact distance ordering is worth the cost.

### Two traps

**The primary key must contain the partition key.** `(id)` became
`(id, tenant_id)`.

**RLS is not inherited by partitions.** A policy on the parent governs rows
reached *through* the parent. `SELECT * FROM chunk_p3` is subject only to
`chunk_p3`'s own policies — and the application role can name it. Every partition
therefore carries its own `ENABLE`, `FORCE` and policy.

Without that, this migration would have opened a direct-access hole in the exact
control the table exists to enforce. Verified as the application role:

```
no tenant context →  chunk: 0 rows,  chunk_p8 (direct): 0 rows
demo-acme context →  chunk: 46,      chunk_p8 (direct): 46
```

### The property suite was silently weakened

A partitioned parent is `relkind = 'p'`, not `'r'`. Two catalog-scanning tests
filtered to `'r'`, so after the migration they **dropped `chunk` from the check
entirely** while admitting its sixteen partitions as unclassified tables.

Both now scan `('r','p')` and collapse partitions onto their parent for the
declared-list comparison, while still asserting each partition individually — 17
relations checked instead of 1. Confirmed it still bites:
`ALTER TABLE chunk_p8 NO FORCE ROW LEVEL SECURITY` fails the suite by name.

### The lexical half had no index either

Graph mode fuses three signals — dense (cosine over pgvector), lexical, and
knowledge-graph entity matching. The vectors were indexed. The lexical half never
had been, so Postgres computed `to_tsvector` for **every row on every query**,
twice: once to filter, once to rank.

Migration 0015 adds a GIN expression index. An honest caveat: at 48 rows the
planner still prefers the cheaper btree, so this is a scaling fix verified
structurally, not one observed firing.

---

## Part 4 — Editions of a corpus

### The model

A document's identity became `(tenant, collection, filename)`; the content hash
became the *version* of that document. `collection_build` names which build a
collection serves.

Writes go to build N+1 while reads stay on N. Promotion is one transaction, so a
rebuild that dies half-written leaves the previous corpus serving — rather than
the reference build's behaviour, where recreating an index in place leaves the
tenant with nothing to serve and a rollback story of "re-ingest and wait".

Rebuilds **copy forward**: unchanged chunks are `INSERT … SELECT` with a new
`build_version`. No model call, no cost. Only genuinely new content needs
embedding.

### At most two builds may coexist

This is a correctness constraint, not housekeeping. Reads carry a
`build_version` predicate that the HNSW index does not contain, so it is a
post-filter — the same defect partitioning was written to fix. Two builds means
the filter discards at most half the candidates, which `iterative_scan` absorbs.
Several builds would quietly gut recall again.

The reaper enforces the bound.

### The trigger chain

Nothing polls for changes. The trigger is written **in the same transaction as
the change**:

```
POST /api/documents  (or DELETE)
   ├─ 1. write / supersede the document row
   └─ 2. enqueue_run(...) → a `run` row + an `outbox` row
        ↑ both commit together, or neither does

relay  ── reads unpublished outbox rows (FOR UPDATE SKIP LOCKED)
       └─ publishes to Redis → Celery → execute_run
                                        └─ leases the run, executes
```

If the queue write happened *after* the commit, a crash in between would leave a
changed document with no rebuild scheduled — silently stale forever. A lease
returns work abandoned by a dead worker; a sweeper catches messages the broker
lost, so the system is not merely as reliable as Redis.

### A live walkthrough

Every value below is from one real run.

**1. Upload `pump-spec.md`**

```
API:  unchanged=False  replaced=None  reindex queued=7daab93b
      document rows : bceef9f4  CURRENT
      builds        : v4 live (40 chunks)
      last run      : pending          ← queued, not yet executed
```

Queuing is not doing. Once a worker picked it up:

```
leased run 7daab93b (reindex, attempt 1/3)
promoted demo-acme/kg-demo to build 5 (40 chunks)
```

The 40 existing chunks were copied into build 5, not reprocessed.

**2. Upload the identical bytes again**

```
API:  unchanged=True   (no rebuild queued at all)
```

Nothing moved. A double-click costs nothing.

**3. Same filename, changed content**

```
API:  new id=701993f2  replaced=bceef9f4
      bceef9f4  withdrawn → 701993f2
      701993f2  CURRENT
      builds    : v5 superseded, v6 live
```

The old row was retired and given a pointer to its replacement — not overwritten,
not deleted.

**4. Delete**

```
API:  "withdrawn; still served by the live build until the rebuild promotes"
      builds : v6 superseded, v7 live
      history view : 2 rows      current view : 0 rows
```

The content was still being served at the moment of deletion, and left only when
build 7 swapped in. Nothing vanished mid-query.

### Findings along the way

**The seeders modelled documents wrongly.** 49 document rows for 48 chunks — one
row per *chunk*, so a two-chunk file became two rows sharing a filename.
Content-hash identity hid it; filename identity broke on it immediately.

The migration repairs it by **repointing chunks** at a surviving row rather than
superseding the duplicates. Superseding would leave live chunks owned by a
superseded document, and the next rebuild — which reads only current documents —
would silently drop that content.

**Supersede before insert, not after.** The unique index is partial on
`superseded_at IS NULL` and is checked per statement, so inserting first leaves
two current rows for one filename and is rejected. The first version had this
backwards, and the code comment asserted the opposite of what happened.

**Five read sites, not three.** `load_documents` (which feeds the knowledge graph
and BM25) and the eval gate's retriever both read chunks. Missing `load_documents`
would have been worse than missing a ranked path: the graph would build from every
build at once and entity counts would silently inflate.

---

## Defects the existing suites caught

Three bugs were caught by tests already in the repository, which is the strongest
evidence that the discipline works.

| Test | What it caught |
|---|---|
| `test_no_bind_parameter_is_followed_by_a_cast` | Three `:name::jsonb` binds. SQLAlchemy claims the first colon, so all three would have been runtime syntax errors |
| `populated_every_table` | New tables had no fixture rows, so isolation assertions over them would have been vacuous |
| `mutation_check.py` | Every control re-proved load-bearing after the capability set and schema changed |

### And one the author wrote badly

The first version of `test_the_reaper_never_deletes_the_live_build` asserted
`remaining <= MAX_COEXISTING_BUILDS` — reading the very constant it was guarding.
Raising that constant from 2 to 99 reopens the recall problem, and the test stayed
**green**. Three of four mutations caught; this one survived silently.

It now asserts against a **literal** `2`, and separately that the constant is
still within it. Four of four mutations now fail the suite.

An assertion that reads what it guards moves its own goalposts. It is the
mutation-facing sibling of a vacuous precondition: in one the pass state is
indistinguishable from having nothing to check, in the other it is
indistinguishable from the control being switched off.

---

## What is not done

> **Closed on 2026-08-14 (Phase 10).** Uploads are now written to MinIO under a
> derived, re-checked tenant key before the row is inserted, and `reindex`
> chunks and embeds any current document contributing nothing to the new build.
> The paragraph below is left as written because it was the honest state at the
> time; see ROLLOUT.md Phase 10 for what replaced it. What remains open from it:
> `.pdf` / `.docx` / `.xlsx` are now *refused* at upload rather than accepted and
> silently unindexable, and stored objects are never reclaimed.
>
> ~~**Uploaded bytes are not retained, and upload does not chunk.**~~
> `POST /api/documents` validated and hashed a file but stored neither the
> content nor any searchable pieces — there was no object-store adapter, only the
> port. A document whose *content* changed therefore could not be re-chunked; it
> dropped its old chunks and contributed none. Every rebuild reported
> `documents_without_chunks` rather than claiming a complete corpus. This was the
> most important remaining gap: until it closed, the lifecycle machinery was
> protecting an empty envelope.

**Objects are never reclaimed.** A superseded document keeps its bytes so a
revert to the previous build does not need a re-upload — that build's chunks are
only reproducible while their content exists. There is no purge path, so storage
grows monotonically with every replacement.

**Tenant isolation in the object store is application-level.** Keys are derived
from the tenant id and re-checked on every operation, with property tests and a
mutation behind it. A bucket policy would be the stronger control, and the port's
own docstring says so; a caller that bypasses the adapter bypasses the boundary.

**A rebuild copies the whole collection for a one-file change.** Copy-forward
avoids re-embedding, which is the expensive part, but not the row copying — at
100k vectors that is roughly 600MB–1GB per upload, and fifty uploads in a morning
means fifty full copies. Two fixes, in order of value: debounce rebuilds into a
window; then question whether collection-wide builds are needed at all, versus
per-document currency resolved by a join.

**Development and production exercise different paths.** `make worker` polls the
database directly; production goes outbox → relay → Redis → Celery. The mechanism
that provides the delivery guarantee is the one the daily workflow never runs.

**No compose service for the console** — `make web` runs it. **Two Next
advisories accepted.** **Onboarding artifacts are not auto-invalidated** when the
corpus drifts; drift is surfaced (`current` / `drifted` / `unknown`) and left to a
person, because re-drafting spends real budget and needs an approval.

---

## Running it

```bash
make up            # substrate: postgres, redis, minio, otel, jaeger, prometheus, grafana
make migrate       # migrations 0001–0016
make init-store    # create the object store bucket — uploads 503 without it
make seed-chat     # two demo tenants, three principals each
make seed-kg       # a 40-chunk corpus built so relations recur
make api           # :8100
make worker        # required for onboarding drafts and rebuilds
make web           # console on :3000
```

Sign in at `http://localhost:3000` with tenant `demo-acme`, password
`demo-password-1234`, and one of `owner@acme.example` (full console),
`reviewer@acme.example` (approves schemas, cannot draft) or
`operator@acme.example` (chat only — admin panels render locked).

> **Kill every worker before running the test suites.** A polling worker leases
> the runs the chaos tests intend to crash and resume themselves, which fails
> three cases and makes the mutation harness report a red baseline. Nothing is
> wrong with the code when that happens.

### Verification state

| Check | Result |
|---|---|
| Property + chaos suites | **95 passed** |
| Mutation harness | **all 23 controls load-bearing** |
| Reference trees (`graphrag-azure/`, engine) | **zero files modified** |

The engine tree does show ~15 modified files dated 2026-08-08 to 2026-08-11 —
pre-existing work, unrelated to this. Since `graphrag-azure/` is not a git
repository, the reliable check there is modification time, not `git status`.
