"""Prove the property tests are load-bearing.

A property test that passes is only interesting if it would fail when the
control it checks is removed. Otherwise the suite is measuring its own
existence: green on a platform with no isolation at all.

So each mutation here disables exactly one control, runs the suite, and asserts
that the suite goes red. A mutation that leaves the suite green is reported as a
**gap in the tests**, not a success — it means the property is currently
unverified regardless of how the run looks.

Every control is restored afterwards, including on failure.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"
ENGINE = ROOT / "platform_core" / "db" / "engine.py"
POLICY = ROOT / "platform_core" / "api" / "policy.py"
ROUTE_TABLE = ROOT / "platform_core" / "api" / "route_table.py"
CAPABILITIES = ROOT / "platform_core" / "identity" / "capabilities.py"
SIDE_EFFECTS = ROOT / "platform_core" / "correctness" / "side_effects.py"

# Every source file a mutation touches, backed up and restored as a set so a
# crash mid-run cannot leave a weakened control in the tree.
LEASES = ROOT / "platform_core" / "correctness" / "leases.py"
LEDGER = ROOT / "platform_core" / "observability" / "ledger.py"
LLM = ROOT / "platform_core" / "observability" / "llm.py"
AUDIT = ROOT / "platform_core" / "observability" / "audit.py"
PROMOTION = ROOT / "platform_core" / "gates" / "promotion.py"
DATASETS = ROOT / "platform_core" / "gates" / "datasets.py"
CANARY = ROOT / "platform_core" / "release" / "canary.py"
SESSIONS = ROOT / "platform_core" / "scaling" / "sessions.py"
OBJECT_STORE = ROOT / "platform_core" / "adapters" / "local" / "object_store.py"
REINDEX = ROOT / "workloads" / "reindex" / "workload.py"
ONBOARDING_STORE = ROOT / "workloads" / "onboarding" / "store.py"
EVAL_WORKLOAD = ROOT / "workloads" / "eval" / "workload.py"
SETTINGS = ROOT / "platform_core" / "settings.py"
EVAL_RUNNER = ROOT / "platform_core" / "gates" / "runner.py"
EVAL_LABELS = ROOT / "platform_core" / "gates" / "labels.py"
ANNOTATOR = ROOT / "platform_core" / "gates" / "annotator.py"
ONBOARDING_ROUTES = ROOT / "platform_core" / "api" / "routes" / "onboarding.py"
HEALTH_ROUTES = ROOT / "platform_core" / "api" / "routes" / "health.py"
ONBOARDING_WORKLOAD = ROOT / "workloads" / "onboarding" / "workload.py"
# Phase 15 control surface. These arrived with the governed-tool runtime,
# admission control and governance sweeps, and were the largest block of the
# tree whose property tests nothing had yet shown to be load-bearing.
TOOLS = ROOT / "platform_core" / "agent" / "tools.py"
RATE_LIMIT = ROOT / "platform_core" / "security" / "rate_limit.py"
OUTBOX = ROOT / "platform_core" / "correctness" / "outbox.py"
MUTABLE_FILES = (ENGINE, POLICY, ROUTE_TABLE, CAPABILITIES, SIDE_EFFECTS, LEASES,
                 LEDGER, LLM, AUDIT, PROMOTION, DATASETS, CANARY, SESSIONS,
                 OBJECT_STORE, REINDEX, ONBOARDING_STORE, EVAL_WORKLOAD,
                 SETTINGS, EVAL_RUNNER, EVAL_LABELS, ANNOTATOR,
                 ONBOARDING_ROUTES, HEALTH_ROUTES, ONBOARDING_WORKLOAD,
                 TOOLS, RATE_LIMIT, OUTBOX)

PSQL = [
    "psql", "-h", "127.0.0.1", "-p", "5442",
    "-U", "platform_owner", "-d", "platform", "-qc",
]
PGENV = {"PGPASSWORD": "platform_dev_only", "PATH": "/usr/bin:/bin"}
SUITE_TIMEOUT_SECONDS = 180


@dataclass
class Mutation:
    name: str
    why: str
    apply: callable
    revert: callable
    # Which suites can possibly catch this. Running the 32-second chaos suite
    # against a mutation only the fast property tests can detect turned a
    # two-minute check into an eleven-minute one, and a check nobody runs
    # because it is slow is a check that is not run.
    suites: tuple[str, ...] = ("tests/properties",)


# Correctness controls are only observable by crashing a real worker.
CHAOS = ("tests/properties", "tests/chaos")
# Admission control and registration-time invariants are pure functions of
# settings and a spec, so they are proven in the unit suite. Running the
# database-backed property suite against them would only add ~30s of unrelated
# work to every one of these mutations.
UNIT = ("tests/unit",)


def _sql(statement: str) -> None:
    subprocess.run([*PSQL, statement], check=True, env=PGENV, capture_output=True)


def _patch_file(path: Path, old: str, new: str) -> None:
    """Replace exactly one occurrence, or fail loudly.

    A mutation that silently matches nothing reports "NOT CAUGHT" and looks like
    a gap in the tests, sending the reader to investigate a test that is fine.
    """
    text = path.read_text()
    if text.count(old) != 1:
        raise RuntimeError(
            f"mutation target appears {text.count(old)} times in {path.name}; "
            f"expected exactly 1. The mutation is stale, not the test."
        )
    path.write_text(text.replace(old, new, 1))


def _leftover_markers() -> list[tuple[str, int]]:
    """Applied mutations left behind by a crashed or killed run.

    A timeout killed a run mid-mutation once and left a control disabled in the
    tree; the suite stayed green because the mutation was one the fast tests do
    not cover. Refusing to start on a dirty tree is cheaper than discovering it
    later.
    """
    found: list[tuple[str, int]] = []
    for path in MUTABLE_FILES:
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if "# mutation:" in line:
                found.append((path.relative_to(ROOT).as_posix(), number))
    return found


def _patch_engine(old: str, new: str) -> None:
    _patch_file(ENGINE, old, new)


def _suite_is_red(suites: tuple[str, ...] = ("tests/properties",)) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [str(PY), "-m", "pytest", *suites, "-q", "--no-header"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=SUITE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return True, f"timed out after {SUITE_TIMEOUT_SECONDS}s"
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    lines = output.splitlines()
    if proc.returncode != 0:
        return True, "\n".join(lines[-25:]) if lines else "(no output)"
    return False, lines[-1] if lines else "(no output)"


MUTATIONS = [
    Mutation(
        name="rls-disabled-on-chunk",
        why="If row-level security is off, cross-tenant reads must be observable.",
        apply=lambda: _sql("ALTER TABLE chunk DISABLE ROW LEVEL SECURITY"),
        revert=lambda: _sql("ALTER TABLE chunk ENABLE ROW LEVEL SECURITY"),
    ),
    Mutation(
        name="permissive-with-check",
        why=(
            "WITH CHECK (true) accepts any post-image, so a tenant can INSERT "
            "under another tenant's id and UPDATE its rows to transfer them. "
            "Note the subtlety: merely *omitting* WITH CHECK is NOT this bug — "
            "for a FOR ALL policy Postgres reuses the USING expression as the "
            "check. An explicit permissive clause is what actually opens it."
        ),
        apply=lambda: (
            _sql("DROP POLICY chunk_tenant_isolation ON chunk"),
            _sql(
                "CREATE POLICY chunk_tenant_isolation ON chunk FOR ALL "
                "TO platform_app, platform_readonly "
                "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
                "WITH CHECK (true)"
            ),
        ),
        revert=lambda: (
            _sql("DROP POLICY chunk_tenant_isolation ON chunk"),
            _sql(
                "CREATE POLICY chunk_tenant_isolation ON chunk FOR ALL "
                "TO platform_app, platform_readonly "
                "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
                "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
            ),
        ),
    ),
    Mutation(
        name="session-scoped-tenant-guc",
        why=(
            "set_config(..., is_local=false) leaves the tenant on the pooled "
            "connection. This is the trap that passes single-tenant testing and "
            "leaks in production; the pool test must catch it."
        ),
        apply=lambda: _patch_engine(
            "set_config(:guc, :tenant_id, true)", "set_config(:guc, :tenant_id, false)"
        ),
        revert=lambda: _patch_engine(
            "set_config(:guc, :tenant_id, false)", "set_config(:guc, :tenant_id, true)"
        ),
    ),
    Mutation(
        name="app-role-granted-bypassrls",
        why=(
            "BYPASSRLS makes every policy decorative while leaving pg_policies "
            "fully populated. Only a capability assertion can detect it."
        ),
        apply=lambda: _sql("ALTER ROLE platform_app BYPASSRLS"),
        revert=lambda: _sql("ALTER ROLE platform_app NOBYPASSRLS"),
    ),
    Mutation(
        name="empty-route-table",
        why=(
            "The real bug this platform already hit: under FastAPI 0.141 the "
            "obvious route enumeration returns nothing, the middleware matches "
            "nothing, and every request is permitted while all functional tests "
            "still pass. An empty table must be caught, not tolerated."
        ),
        apply=lambda: _patch_file(
            ROUTE_TABLE, "for router in ROUTERS:", "for router in ROUTERS[:0]:"
        ),
        revert=lambda: _patch_file(
            ROUTE_TABLE, "for router in ROUTERS[:0]:", "for router in ROUTERS:"
        ),
    ),
    Mutation(
        name="everything-public",
        why=(
            "Widening PUBLIC_PATHS to match any path turns default-deny into "
            "default-allow. The anonymous-rejection sweep must catch it."
        ),
        apply=lambda: _patch_file(
            POLICY, "def is_public(path: str) -> bool:\n    return path in PUBLIC_PATHS",
            "def is_public(path: str) -> bool:\n    return True",
        ),
        revert=lambda: _patch_file(
            POLICY, "def is_public(path: str) -> bool:\n    return True",
            "def is_public(path: str) -> bool:\n    return path in PUBLIC_PATHS",
        ),
    ),
    Mutation(
        name="readiness-exposes-database-dsn",
        why=(
            "A public readiness probe carrying a resolved DSN discloses its "
            "password and topology to every anonymous caller."
        ),
        apply=lambda: _patch_file(
            HEALTH_ROUTES,
            '        "checks": checks,\n    }',
            '        "checks": checks,\n        "config": {"database_url": str(settings.database_url)},'
            '\n        # mutation: public configuration disclosure\n    }',
        ),
        revert=lambda: _patch_file(
            HEALTH_ROUTES,
            '        "checks": checks,\n        "config": {"database_url": str(settings.database_url)},'
            '\n        # mutation: public configuration disclosure\n    }',
            '        "checks": checks,\n    }',
        ),
    ),
    Mutation(
        name="relay-privilege-as-a-flag",
        why=(
            "Re-introduces the hole opened during Phase 2: cross-tenant access "
            "granted by a session variable instead of a credential, so any code "
            "path that opens a system session — including login — reads every "
            "tenant's runs."
        ),
        apply=lambda: (
            _sql(
                "CREATE POLICY run_flag_access ON run FOR ALL TO platform_app "
                "USING (NULLIF(current_setting('app.system_reason', true), '') IS NOT NULL) "
                "WITH CHECK (NULLIF(current_setting('app.system_reason', true), '') IS NOT NULL)"
            ),
        ),
        revert=lambda: (_sql("DROP POLICY IF EXISTS run_flag_access ON run"),),
    ),
    Mutation(
        name="side-effect-uniqueness-dropped",
        suites=CHAOS,
        why=(
            "The UNIQUE (run_id, step) constraint IS the idempotency mechanism — "
            "the INSERT is the claim. Without it two attempts both execute, and "
            "a non-idempotent effect is applied twice."
        ),
        apply=lambda: _sql(
            "ALTER TABLE side_effect DROP CONSTRAINT side_effect_run_step_uniq"
        ),
        revert=lambda: _sql(
            "ALTER TABLE side_effect ADD CONSTRAINT side_effect_run_step_uniq "
            "UNIQUE (run_id, step)"
        ),
    ),
    Mutation(
        name="reaper-ignores-lease-expiry",
        suites=CHAOS,
        why=(
            "A reaper that returns *live* leases to pending manufactures the "
            "double-execution leases exist to prevent. The live-lease test must "
            "catch it."
        ),
        apply=lambda: _patch_file(
            ROOT / "platform_core" / "correctness" / "leases.py",
            "WHERE status = 'leased' AND lease_expires_at < now() - :grace ",
            "WHERE status = 'leased' AND lease_expires_at < now() + interval '1 day' - :grace ",
        ),
        revert=lambda: _patch_file(
            ROOT / "platform_core" / "correctness" / "leases.py",
            "WHERE status = 'leased' AND lease_expires_at < now() + interval '1 day' - :grace ",
            "WHERE status = 'leased' AND lease_expires_at < now() - :grace ",
        ),
    ),
    Mutation(
        name="non-idempotent-effect-retried",
        suites=CHAOS,
        why=(
            "Treating NEEDS_RECONCILIATION as safe-to-repeat means a crash "
            "between the effect and its completion record doubles the effect. "
            "The mid-step chaos crash must catch it."
        ),
        apply=lambda: _patch_file(
            SIDE_EFFECTS,
            "            if retry_policy is RetryPolicy.NEEDS_RECONCILIATION:",
            "            if False:  # mutation: reconciliation policy ignored",
        ),
        revert=lambda: _patch_file(
            SIDE_EFFECTS,
            "            if False:  # mutation: reconciliation policy ignored",
            "            if retry_policy is RetryPolicy.NEEDS_RECONCILIATION:",
        ),
    ),
    Mutation(
        name="budget-reports-instead-of-refusing",
        why=(
            "A ceiling that records spend but never raises is a receipt, not a "
            "control — the exact state the Azure ledger was in for months."
        ),
        apply=lambda: _patch_file(
            LEDGER,
            "        if status.exhausted or projected >= status.daily_token_cap:",
            "        if False:  # mutation: budget reports but never refuses",
        ),
        revert=lambda: _patch_file(
            LEDGER,
            "        if False:  # mutation: budget reports but never refuses",
            "        if status.exhausted or projected >= status.daily_token_cap:",
        ),
    ),
    Mutation(
        name="calls-without-a-tenant-permitted",
        why=(
            "Dropping the identity link lets a call proceed with no owner, which "
            "is how spend becomes unattributable in the first place."
        ),
        apply=lambda: _patch_file(
            LLM,
            "    if ctx is None or ctx.principal is None or ctx.tenant is None:",
            "    if False:  # mutation: unattributable calls permitted",
        ),
        revert=lambda: _patch_file(
            LLM,
            "    if False:  # mutation: unattributable calls permitted",
            "    if ctx is None or ctx.principal is None or ctx.tenant is None:",
        ),
    ),
    Mutation(
        name="everything-fails-open",
        why=(
            "Making every task fail open on an unreadable ledger discards the "
            "per-task policy: a corpus rebuild would spend uncapped AND unrecorded."
        ),
        apply=lambda: _patch_file(
            LEDGER, "    return task in INTERACTIVE_TASKS",
            "    return True  # mutation: every task fails open",
        ),
        revert=lambda: _patch_file(
            LEDGER, "    return True  # mutation: every task fails open",
            "    return task in INTERACTIVE_TASKS",
        ),
    ),
    Mutation(
        name="audit-chain-never-verified",
        why=(
            "A verify_chain that always reports intact makes the hash chain "
            "decorative — tampering would be undetectable while every audit "
            "test still passed."
        ),
        apply=lambda: _patch_file(
            AUDIT,
            "        if recomputed != row.hash:",
            "        if False:  # mutation: digest mismatch ignored",
        ),
        revert=lambda: _patch_file(
            AUDIT,
            "        if False:  # mutation: digest mismatch ignored",
            "        if recomputed != row.hash:",
        ),
    ),
    Mutation(
        name="unpriced-models-are-free",
        why=(
            "Charging zero for an unknown model reads as thrift in every report "
            "and hides the cost of any newly adopted model."
        ),
        apply=lambda: _patch_file(
            LLM, "UNKNOWN_MODEL_PRICING = (0.0025, 0.01)",
            "UNKNOWN_MODEL_PRICING = (0.0, 0.0)  # mutation: unpriced is free",
        ),
        revert=lambda: _patch_file(
            LLM, "UNKNOWN_MODEL_PRICING = (0.0, 0.0)  # mutation: unpriced is free",
            "UNKNOWN_MODEL_PRICING = (0.0025, 0.01)",
        ),
    ),
    Mutation(
        name="gate-never-blocks",
        why=(
            "A gate that always promotes is the Azure situation exactly: both "
            "metrics computed correctly, nothing able to refuse anything."
        ),
        apply=lambda: _patch_file(
            PROMOTION, "        if delta < -tolerance:",
            "        if False:  # mutation: regressions never block",
        ),
        revert=lambda: _patch_file(
            PROMOTION, "        if False:  # mutation: regressions never block",
            "        if delta < -tolerance:",
        ),
    ),
    Mutation(
        name="incomparable-datasets-compared",
        why=(
            "Comparing a candidate against a baseline scored on different "
            "questions produces a confident wrong answer: both numbers are real, "
            "the comparison is not."
        ),
        apply=lambda: _patch_file(
            PROMOTION,
            '    if baseline["dataset_sha"] != candidate.dataset_sha:',
            "    if False:  # mutation: dataset identity ignored",
        ),
        revert=lambda: _patch_file(
            PROMOTION,
            "    if False:  # mutation: dataset identity ignored",
            '    if baseline["dataset_sha"] != candidate.dataset_sha:',
        ),
    ),
    Mutation(
        name="unreachable-citations-accepted",
        why=(
            "A must_cite the retriever can never emit scores a permanent miss "
            "and halves that item's recall, indistinguishably from a real "
            "retrieval failure."
        ),
        apply=lambda: _patch_file(
            DATASETS,
            "        bad = [c for c in must_cite if not CANONICAL_ID.match(c)]",
            "        bad = []  # mutation: citation format unchecked",
        ),
        revert=lambda: _patch_file(
            DATASETS,
            "        bad = []  # mutation: citation format unchecked",
            "        bad = [c for c in must_cite if not CANONICAL_ID.match(c)]",
        ),
    ),
    Mutation(
        name="unscoreable-run-treated-as-pass",
        why=(
            "A run producing no metric must not read as no-regression. This is "
            "the vacuity failure this codebase hit three times in other forms."
        ),
        apply=lambda: _patch_file(
            PROMOTION,
            "    if candidate.retrieval_recall is None and candidate.answer_pass_rate is None:",
            "    if False:  # mutation: unscoreable runs pass",
        ),
        revert=lambda: _patch_file(
            PROMOTION,
            "    if False:  # mutation: unscoreable runs pass",
            "    if candidate.retrieval_recall is None and candidate.answer_pass_rate is None:",
        ),
    ),
    Mutation(
        name="canary-never-rolls-back",
        why=(
            "A canary that detects a breach but does not act has the same mean "
            "time to recovery as no canary — it just fails on less traffic."
        ),
        apply=lambda: _patch_file(
            CANARY, "    if verdict.action == \"rollback\":",
            "    if False:  # mutation: breaches never trigger a rollback",
        ),
        revert=lambda: _patch_file(
            CANARY, "    if False:  # mutation: breaches never trigger a rollback",
            "    if verdict.action == \"rollback\":",
        ),
    ),
    Mutation(
        name="canary-promotes-on-tiny-sample",
        why=(
            "Without a minimum sample a canary promotes itself seconds after "
            "starting, before it has served enough traffic to fail."
        ),
        apply=lambda: _patch_file(
            CANARY, "    if candidate.observations < min_observations:",
            "    if False:  # mutation: sample size ignored",
        ),
        revert=lambda: _patch_file(
            CANARY, "    if False:  # mutation: sample size ignored",
            "    if candidate.observations < min_observations:",
        ),
    ),
    Mutation(
        name="session-not-bound-to-principal",
        why=(
            "Dropping the principal check makes a session id a complete "
            "credential — anyone holding it reads the conversation, which is "
            "the Azure GET /api/sessions/{id} exposure."
        ),
        apply=lambda: _patch_file(
            SESSIONS,
            '                "WHERE id = :id AND principal_id = :p AND expires_at > now()"',
            '                "WHERE id = :id AND expires_at > now()"  # mutation: unbound',
        ),
        revert=lambda: _patch_file(
            SESSIONS,
            '                "WHERE id = :id AND expires_at > now()"  # mutation: unbound',
            '                "WHERE id = :id AND principal_id = :p AND expires_at > now()"',
        ),
    ),
    Mutation(
        name="self-approval-permitted",
        why=(
            "Dropping the maker-cannot-be-checker rule from the capability "
            "layer. The database CHECK constraint is the real guarantee, so "
            "this should surface as a failed request either way — but the "
            "application-level refusal is what makes it a 403 rather than a 500."
        ),
        apply=lambda: _patch_file(
            CAPABILITIES,
            "if capability in SELF_APPROVAL_FORBIDDEN and subject_principal_id == principal.id:",
            "if False:  # mutation: self-approval guard disabled",
        ),
        revert=lambda: _patch_file(
            CAPABILITIES,
            "if False:  # mutation: self-approval guard disabled",
            "if capability in SELF_APPROVAL_FORBIDDEN and subject_principal_id == principal.id:",
        ),
    ),
    Mutation(
        name="object-key-scope-unchecked",
        why=(
            "Trusting the key a caller passes instead of re-deriving its scope. "
            "key_for would still produce safe keys, so every happy path stays "
            "green — and any tenant holding another's storage_key could read, "
            "overwrite or delete its documents."
        ),
        apply=lambda: _patch_file(
            OBJECT_STORE,
            '        if not key.startswith(prefix) or ".." in key:',
            "        if False:  # mutation: tenant prefix check disabled",
        ),
        revert=lambda: _patch_file(
            OBJECT_STORE,
            "        if False:  # mutation: tenant prefix check disabled",
            '        if not key.startswith(prefix) or ".." in key:',
        ),
    ),
    Mutation(
        name="conditional-write-ignored",
        why=(
            "Dropping If-None-Match turns every conditional put into a blind "
            "overwrite. Nothing raises, so a retried step silently replaces the "
            "bytes a concurrent worker committed — the idempotency primitive in "
            "side_effects.py is built on this holding."
        ),
        apply=lambda: _patch_file(
            OBJECT_STORE,
            "        if if_absent:\n            kwargs[\"IfNoneMatch\"] = \"*\"",
            "        if False:  # mutation: conditional write ignored\n"
            "            kwargs[\"IfNoneMatch\"] = \"*\"",
        ),
        revert=lambda: _patch_file(
            OBJECT_STORE,
            "        if False:  # mutation: conditional write ignored\n"
            "            kwargs[\"IfNoneMatch\"] = \"*\"",
            "        if if_absent:\n            kwargs[\"IfNoneMatch\"] = \"*\"",
        ),
    ),
    Mutation(
        name="ingest-step-skipped",
        why=(
            "Reverting reindex to copy-forward only. Every rebuild still "
            "succeeds and promotes; a newly uploaded or replaced document simply "
            "contributes nothing. That is the defect this phase closed, and it "
            "is invisible in any assertion about whether the build went live."
        ),
        apply=lambda: _patch_file(
            REINDEX,
            '    if not pending:\n        return {"ingested_documents": 0, '
            '"ingested_chunks": 0, "skipped": {}}',
            '    if True:  # mutation: ingest disabled\n'
            '        return {"ingested_documents": 0, '
            '"ingested_chunks": 0, "skipped": {}}',
        ),
        revert=lambda: _patch_file(
            REINDEX,
            '    if True:  # mutation: ingest disabled\n'
            '        return {"ingested_documents": 0, '
            '"ingested_chunks": 0, "skipped": {}}',
            '    if not pending:\n        return {"ingested_documents": 0, '
            '"ingested_chunks": 0, "skipped": {}}',
        ),
    ),
    Mutation(
        name="editor-may-approve-their-own-edit",
        why=(
            "Dropping the editor check from approve(). The drafter is still "
            "refused, so the original maker/checker test stays green — and one "
            "principal could rewrite a taxonomy and sign off their own rewrite, "
            "which is the unilateral path to production the rule exists to close."
        ),
        apply=lambda: _patch_file(
            ONBOARDING_STORE,
            "        if row.schema_edited_by == ctx.principal.id:",
            "        if False:  # mutation: editor may self-approve",
        ),
        revert=lambda: _patch_file(
            ONBOARDING_STORE,
            "        if False:  # mutation: editor may self-approve",
            "        if row.schema_edited_by == ctx.principal.id:",
        ),
    ),
    Mutation(
        name="schema-resolution-ignores-artifact-name",
        why=(
            "Resolving a singleton artifact by kind alone. Every session with "
            "one schema row behaves identically, so nothing looks wrong — but a "
            "session that has been edited has two, and which one gets published "
            "then depends on the order the rows come back."
        ),
        apply=lambda: _patch_file(
            ONBOARDING_STORE,
            "        elif r.kind in SINGLETON_KINDS and r.name == r.kind:",
            "        elif r.kind in SINGLETON_KINDS:  # mutation: name ignored",
        ),
        revert=lambda: _patch_file(
            ONBOARDING_STORE,
            "        elif r.kind in SINGLETON_KINDS:  # mutation: name ignored",
            "        elif r.kind in SINGLETON_KINDS and r.name == r.kind:",
        ),
    ),
    Mutation(
        name="eval-worker-promotes-its-own-candidate",
        why=(
            "Letting the workload move the baseline instead of only reporting "
            "the verdict. Every score stays correct and every gate reason is "
            "still computed — the run simply becomes the thing it is measured "
            "against, so no regression can ever be detected again."
        ),
        apply=lambda: _patch_file(
            EVAL_WORKLOAD,
            "    decision = promotion.evaluate(ctx, completed, dataset_name=dataset_name)",
            "    decision = promotion.promote(ctx, completed, "
            "dataset_name=dataset_name)  # mutation: self-promoting",
        ),
        revert=lambda: _patch_file(
            EVAL_WORKLOAD,
            "    decision = promotion.promote(ctx, completed, "
            "dataset_name=dataset_name)  # mutation: self-promoting",
            "    decision = promotion.evaluate(ctx, completed, dataset_name=dataset_name)",
        ),
    ),
    Mutation(
        name="dataset-writes-drop-to-run-authority",
        why=(
            "Gating golden-set authorship on eval:run instead of "
            "release:promote. Nothing fails and nobody is locked out — and "
            "anyone who may measure may now rewrite the questions, which makes "
            "any regression passable."
        ),
        apply=lambda: _patch_file(
            POLICY,
            '    ("PUT", "/api/eval/datasets/{name}"): RoutePolicy('
            "Capability.RELEASE_PROMOTE),",
            '    ("PUT", "/api/eval/datasets/{name}"): RoutePolicy('
            "Capability.EVAL_RUN),  # mutation: weakened",
        ),
        revert=lambda: _patch_file(
            POLICY,
            '    ("PUT", "/api/eval/datasets/{name}"): RoutePolicy('
            "Capability.EVAL_RUN),  # mutation: weakened",
            '    ("PUT", "/api/eval/datasets/{name}"): RoutePolicy('
            "Capability.RELEASE_PROMOTE),",
        ),
    ),
    Mutation(
        name="judge-may-be-the-answering-model",
        why=(
            "Dropping the startup refusal that keeps the judge off the model it "
            "grades. Nothing fails and every number stays real — they are simply "
            "flattering for ever, in a direction no report reveals. This is the "
            "control that has no symptom."
        ),
        apply=lambda: _patch_file(
            SETTINGS,
            "        if self.llm_model_judge == self.llm_model_cheap:",
            "        if False:  # mutation: judge independence unchecked",
        ),
        revert=lambda: _patch_file(
            SETTINGS,
            "        if False:  # mutation: judge independence unchecked",
            "        if self.llm_model_judge == self.llm_model_cheap:",
        ),
    ),
    Mutation(
        name="judge-outage-counted-as-failing-answers",
        why=(
            "Folding items the judge could not grade into the pass rate. The "
            "reference deployment shipped exactly this: an unsupported "
            "response_format became 'judge unavailable' and the run reported a "
            "quality collapse while the answers were fine."
        ),
        apply=lambda: _patch_file(
            EVAL_RUNNER,
            "    judged = [o for o in outcomes if o.passed is not None "
            "and not o.judge_unavailable]",
            "    judged = [o for o in outcomes "
            "if o.passed is not None]  # mutation: outage counted",
        ),
        revert=lambda: _patch_file(
            EVAL_RUNNER,
            "    judged = [o for o in outcomes "
            "if o.passed is not None]  # mutation: outage counted",
            "    judged = [o for o in outcomes if o.passed is not None "
            "and not o.judge_unavailable]",
        ),
    ),
    Mutation(
        name="unusable-items-are-scored-anyway",
        why=(
            "Running items a reviewer flagged as having unusable evidence. They "
            "score failures that say nothing about the platform, and the run "
            "reports a lower pass rate with no indication why."
        ),
        apply=lambda: _patch_file(
            EVAL_RUNNER,
            '        if (labels.get(item.id, {}).get("unusable_reason") or "").strip():',
            "        if False:  # mutation: unusable items not excluded",
        ),
        revert=lambda: _patch_file(
            EVAL_RUNNER,
            "        if False:  # mutation: unusable items not excluded",
            '        if (labels.get(item.id, {}).get("unusable_reason") or "").strip():',
        ),
    ),
    Mutation(
        name="rubber-stamped-answers-counted-as-reviewed",
        why=(
            "Reporting every confirmation as SME-attested. A set where nobody "
            "read a single drafted answer then looks identical to one that was "
            "reviewed line by line, and the pass rate measured against it is a "
            "statement about two models agreeing."
        ),
        apply=lambda: _patch_file(
            EVAL_LABELS,
            '        if labels.get(i.id, {}).get("answer_source") == "llm_drafted"\n    )',
            '        if False  # mutation: rubber-stamps hidden\n    )',
        ),
        revert=lambda: _patch_file(
            EVAL_LABELS,
            '        if False  # mutation: rubber-stamps hidden\n    )',
            '        if labels.get(i.id, {}).get("answer_source") == "llm_drafted"\n    )',
        ),
    ),
    Mutation(
        name="annotator-redrafts-over-a-human",
        why=(
            "Letting a second drafting pass overwrite an answer a reviewer "
            "wrote. Re-running drafting then silently replaces ground truth with "
            "a model's guess, and nothing about the set looks different."
        ),
        apply=lambda: _patch_file(
            ANNOTATOR,
            "        and labels.get(item.id, {}).get(\"answer_source\") not in _HUMAN_AUTHORED",
            "        and True  # mutation: human answers not protected",
        ),
        revert=lambda: _patch_file(
            ANNOTATOR,
            "        and True  # mutation: human answers not protected",
            "        and labels.get(item.id, {}).get(\"answer_source\") not in _HUMAN_AUTHORED",
        ),
    ),
    Mutation(
        name="unapproved-questions-seed-anyway",
        why=(
            "Seeding every proposed question instead of the approved ones. The "
            "set is larger and looks richer, nothing errors, and the review step "
            "that was supposed to decide what the domain must answer has been "
            "reduced to decoration."
        ),
        apply=lambda: _patch_file(
            ONBOARDING_ROUTES,
            "        q for q in store.candidate_queries(ctx, session_id) "
            'if q.get("approved")',
            "        q for q in store.candidate_queries(ctx, session_id)  "
            "# mutation: review ignored",
        ),
        revert=lambda: _patch_file(
            ONBOARDING_ROUTES,
            "        q for q in store.candidate_queries(ctx, session_id)  "
            "# mutation: review ignored",
            "        q for q in store.candidate_queries(ctx, session_id) "
            'if q.get("approved")',
        ),
    ),
    Mutation(
        name="candidate-queries-lose-the-canonical-id",
        why=(
            "Dropping the canonical chunk id on the way into the query "
            "generator. Questions still generate and still carry evidence ids — "
            "synthesised ones, in a private namespace the retriever never emits, "
            "so every recall and citation number computed against them reads "
            "0.0. This is the state the reference deployment ships in."
        ),
        apply=lambda: _patch_file(
            ONBOARDING_WORKLOAD,
            '                chunk_id=doc["chunk_id"],',
            '                chunk_id="",  # mutation: canonical id dropped',
        ),
        revert=lambda: _patch_file(
            ONBOARDING_WORKLOAD,
            '                chunk_id="",  # mutation: canonical id dropped',
            '                chunk_id=doc["chunk_id"],',
        ),
    ),
    # ── Phase 15 controls ────────────────────────────────────────────────
    # Governed tools, admission control, outbox delivery and the governance
    # sweeps shipped with property tests but with nothing showing those tests
    # were load-bearing. Everything below closes that.
    Mutation(
        name="approval-is-reusable",
        why=(
            "consumed_at is what makes an approval single-use. Without it one "
            "signature authorises a write tool for as long as the approval has "
            "not expired, which is a standing permission wearing an approval's "
            "name."
        ),
        apply=lambda: _patch_file(
            TOOLS,
            '                    "AND consumed_at IS NULL AND expires_at > now() RETURNING id"\n'
            "                ),",
            '                    "AND expires_at > now() RETURNING id"\n'
            "                ),  # mutation: approval single-use guard removed",
        ),
        revert=lambda: _patch_file(
            TOOLS,
            '                    "AND expires_at > now() RETURNING id"\n'
            "                ),  # mutation: approval single-use guard removed",
            '                    "AND consumed_at IS NULL AND expires_at > now() RETURNING id"\n'
            "                ),",
        ),
    ),
    Mutation(
        name="approval-ignores-argument-hash",
        why=(
            "An approval that does not match on arguments_sha256 approves the "
            "*tool*, not the call. A reviewer signs off on transferring 7 and "
            "the run transfers 8. Note this is invisible to the single-use "
            "test, which consumes the approval first and is then refused by "
            "consumed_at whatever the arguments say — so it needed its own "
            "property test on a live, unconsumed approval."
        ),
        apply=lambda: _patch_file(
            TOOLS,
            '                    "AND arguments_sha256 = :sha AND status = \'approved\' "',
            "                    # mutation: approval no longer bound to its arguments\n"
            '                    "AND status = \'approved\' "',
        ),
        revert=lambda: _patch_file(
            TOOLS,
            "                    # mutation: approval no longer bound to its arguments\n"
            '                    "AND status = \'approved\' "',
            '                    "AND arguments_sha256 = :sha AND status = \'approved\' "',
        ),
    ),
    Mutation(
        name="replay-ignores-tool-identity",
        why=(
            "Idempotent replay must return the stored result only for the same "
            "tool and the same arguments. Drop that check and one idempotency "
            "key silently returns another call's result — the caller believes a "
            "write happened that never did."
        ),
        apply=lambda: _patch_file(
            TOOLS,
            "    if row.tool_name != tool_name or row.arguments_sha256 != arguments_sha:",
            "    if False:  # mutation: replay identity check removed",
        ),
        revert=lambda: _patch_file(
            TOOLS,
            "    if False:  # mutation: replay identity check removed",
            "    if row.tool_name != tool_name or row.arguments_sha256 != arguments_sha:",
        ),
    ),
    Mutation(
        name="write-tools-may-skip-approval",
        suites=UNIT,
        why=(
            "The registry refuses a write tool that does not require approval, "
            "so the gate cannot be forgotten at registration time. Without it "
            "the approval path is opt-in, and the tool that most needs it is "
            "the one whose author was in a hurry."
        ),
        apply=lambda: _patch_file(
            TOOLS,
            '        if self.side_effect == "write" and not self.requires_approval:',
            "        if False:  # mutation: write tools may register ungated",
        ),
        revert=lambda: _patch_file(
            TOOLS,
            "        if False:  # mutation: write tools may register ungated",
            '        if self.side_effect == "write" and not self.requires_approval:',
        ),
    ),
    Mutation(
        name="rate-limit-admits-one-over-quota",
        suites=UNIT,
        why=(
            "An off-by-one in the admission comparison is the failure mode a "
            "rate limiter actually has: it still limits, still reports, and is "
            "wrong by exactly the amount nobody notices until the quota is the "
            "thing standing between a tenant and a bill."
        ),
        apply=lambda: _patch_file(
            RATE_LIMIT,
            "            allowed=count <= limit,",
            "            allowed=count <= limit + 1,  # mutation: quota exceeded by one",
        ),
        revert=lambda: _patch_file(
            RATE_LIMIT,
            "            allowed=count <= limit + 1,  # mutation: quota exceeded by one",
            "            allowed=count <= limit,",
        ),
    ),
    Mutation(
        name="rate-limit-fallback-never-refuses",
        suites=UNIT,
        why=(
            "When Redis is gone the in-process fallback still has to refuse. A "
            "fallback that admits everything turns a cache outage into an "
            "unmetered surface, and it looks identical to a working limiter "
            "from every angle except the one that matters."
        ),
        apply=lambda: _patch_file(
            RATE_LIMIT,
            "        ttl = max(1, int(expires_at - now))\n"
            "        return self._decision(count, ttl, limit, window_seconds)",
            "        ttl = max(1, int(expires_at - now))\n"
            "        # mutation: in-process fallback admits everything\n"
            "        return LimitDecision(True, limit, limit, 0)",
        ),
        revert=lambda: _patch_file(
            RATE_LIMIT,
            "        ttl = max(1, int(expires_at - now))\n"
            "        # mutation: in-process fallback admits everything\n"
            "        return LimitDecision(True, limit, limit, 0)",
            "        ttl = max(1, int(expires_at - now))\n"
            "        return self._decision(count, ttl, limit, window_seconds)",
        ),
    ),
    Mutation(
        name="failed-publish-marked-delivered",
        why=(
            "The `continue` after a publish failure is the entire retry "
            "mechanism. Falling through stamps published_at on a row that was "
            "never delivered, so the run is dropped and the outbox reports a "
            "clean backlog — the worst combination available."
        ),
        apply=lambda: _patch_file(
            OUTBOX,
            '                logger.exception("outbox row %s failed to publish", row.id)\n'
            "                continue",
            '                logger.exception("outbox row %s failed to publish", row.id)\n'
            "                # mutation: failed publish falls through to delivered",
        ),
        revert=lambda: _patch_file(
            OUTBOX,
            '                logger.exception("outbox row %s failed to publish", row.id)\n'
            "                # mutation: failed publish falls through to delivered",
            '                logger.exception("outbox row %s failed to publish", row.id)\n'
            "                continue",
        ),
    ),
    Mutation(
        name="continuous-eval-schedule-is-optional",
        why=(
            "Continuous evaluation is mandatory precisely because it is the "
            "control nobody misses. A dataset saved without a schedule is "
            "measured once, at the moment someone was already paying attention, "
            "and never again."
        ),
        apply=lambda: _patch_file(
            DATASETS,
            "        s.execute(\n            text(\n"
            '                "INSERT INTO continuous_eval_policy "',
            "        None if True else s.execute(  # mutation: schedule not created\n"
            "            text(\n"
            '                "INSERT INTO continuous_eval_policy "',
        ),
        revert=lambda: _patch_file(
            DATASETS,
            "        None if True else s.execute(  # mutation: schedule not created\n"
            "            text(\n"
            '                "INSERT INTO continuous_eval_policy "',
            "        s.execute(\n            text(\n"
            '                "INSERT INTO continuous_eval_policy "',
        ),
    ),
    Mutation(
        name="retention-deletes-audit-without-anchoring",
        why=(
            "Retention is allowed to delete audit history only because it "
            "preserves the chain head first. Suppress the anchor and the "
            "deletion still succeeds, the ledger still looks healthy, and "
            "verification can no longer distinguish aged-out history from "
            "history someone removed."
        ),
        apply=lambda: (
            _sql(
                "CREATE OR REPLACE FUNCTION mutation_skip_anchor() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$"
            ),
            _sql(
                "CREATE TRIGGER mutation_skip_anchor BEFORE INSERT ON audit_chain_anchor "
                "FOR EACH ROW EXECUTE FUNCTION mutation_skip_anchor()"
            ),
        ),
        revert=lambda: (
            _sql("DROP TRIGGER IF EXISTS mutation_skip_anchor ON audit_chain_anchor"),
            _sql("DROP FUNCTION IF EXISTS mutation_skip_anchor()"),
        ),
    ),
]


def main() -> int:
    # The harness captures every child suite, so explicit line buffering makes
    # each completed mutation visible in CI before the overall run finishes.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    backup_dir = Path(tempfile.mkdtemp())
    # Keyed by the path relative to the repo, not by basename. Three files in
    # this list are called `workload.py`; keying on `f.name` collapsed them onto
    # one backup, and the restore then wrote one workload's source over another's
    # — silently, because every mutation had already been reported as caught.
    # Found the hard way; see ROLLOUT.md.
    backups = {f: backup_dir / f.relative_to(ROOT) for f in MUTABLE_FILES}
    for saved in backups.values():
        saved.parent.mkdir(parents=True, exist_ok=True)
    for original, saved in backups.items():
        shutil.copy2(original, saved)

    def restore_all() -> None:
        for original, saved in backups.items():
            shutil.copy2(saved, original)

    stale = _leftover_markers()
    if stale:
        print("refusing to start: a previous run left mutations applied:", file=sys.stderr)
        for path, line in stale:
            print(f"  {path}:{line}", file=sys.stderr)
        return 2

    green, tail = _suite_is_red(CHAOS)
    if green:
        print(f"baseline suite is not passing — fix that first:\n  {tail}", file=sys.stderr)
        return 2
    print(f"baseline: {tail}\n")

    gaps: list[str] = []
    for mutation in MUTATIONS:
        try:
            mutation.apply()
            went_red, tail = _suite_is_red(mutation.suites)
            status = "caught" if went_red else "NOT CAUGHT"
            print(f"[{status:>10}] {mutation.name}")
            print(f"             {mutation.why}")
            print(f"             {tail}\n")
            if not went_red:
                gaps.append(mutation.name)
        finally:
            try:
                mutation.revert()
            except Exception:
                restore_all()
                raise

    restore_all()
    still_green, tail = _suite_is_red(CHAOS)
    if still_green:
        print(f"suite did not return to green after restore: {tail}", file=sys.stderr)
        return 2

    if gaps:
        print(f"UNVERIFIED PROPERTIES: {', '.join(gaps)}", file=sys.stderr)
        print("Each of these controls could be removed without any test noticing.",
              file=sys.stderr)
        return 1

    print(f"all {len(MUTATIONS)} controls are load-bearing; suite restored to green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
