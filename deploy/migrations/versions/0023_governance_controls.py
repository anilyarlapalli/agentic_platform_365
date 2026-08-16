"""continuous evaluation, bounded retention, and audit chain anchors

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-15

The relay remains a narrow delivery credential. Cross-tenant scheduling and
retention are exposed as fixed-signature SECURITY DEFINER functions rather than
granting the relay read/write access to documents, evaluations, sessions, or
the audit ledger.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"
READONLY_ROLE = "platform_readonly"
RELAY_ROLE = "platform_relay"


def _protect(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
            FOR ALL TO {APP_ROLE}, {READONLY_ROLE}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def upgrade() -> None:
    # A dataset without a schedule is an evaluation somebody must remember to
    # run. The row is created with every dataset name and there is deliberately
    # no `enabled` flag: cadence is configurable; whether quality is measured is
    # not.
    op.create_table(
        "continuous_eval_policy",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"), primary_key=True,
        ),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("dataset_name", sa.String(128), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="21600"),
        sa.Column("top_k", sa.SmallInteger(), nullable=False, server_default="5"),
        sa.Column(
            "next_run_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_scheduled_at", sa.DateTime(timezone=True)),
        sa.Column(
            "last_run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("run.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("principal.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "updated_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("principal.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "interval_seconds BETWEEN 900 AND 604800",
            name="continuous_eval_interval_bounded",
        ),
        sa.CheckConstraint("top_k BETWEEN 1 AND 50", name="continuous_eval_top_k_bounded"),
        sa.UniqueConstraint(
            "tenant_id", "dataset_name", name="continuous_eval_policy_dataset_uniq"
        ),
    )
    op.create_index(
        "continuous_eval_due_idx", "continuous_eval_policy", ["next_run_at"]
    )
    _protect("continuous_eval_policy")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON continuous_eval_policy TO {APP_ROLE}"
    )
    op.execute(f"GRANT SELECT ON continuous_eval_policy TO {READONLY_ROLE}")

    # Preserve the head of a hash chain when old audit rows age out. Verification
    # starts from this hash, so retention does not turn tamper evidence off.
    op.create_table(
        "audit_chain_anchor",
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("through_event_id", sa.BigInteger(), nullable=False),
        sa.Column("through_hash", sa.String(64), nullable=False),
        sa.Column("events_anchored", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column(
            "anchored_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    _protect("audit_chain_anchor")
    op.execute(f"GRANT SELECT ON audit_chain_anchor TO {APP_ROLE}, {READONLY_ROLE}")

    # Existing golden sets immediately acquire mandatory schedules. DISTINCT ON
    # chooses only the actor from the latest version; the policy is per name.
    op.execute(
        """
        INSERT INTO continuous_eval_policy
            (tenant_id, dataset_name, created_by, updated_by, next_run_at)
        SELECT DISTINCT ON (tenant_id, name)
            tenant_id, name, created_by, created_by, now()
        FROM eval_dataset
        ORDER BY tenant_id, name, created_at DESC
        ON CONFLICT (tenant_id, dataset_name) DO NOTHING
        """
    )

    op.execute(
        """
        CREATE FUNCTION platform_schedule_due_continuous_evals(
            p_limit integer, p_release text
        ) RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            policy record;
            dataset record;
            scheduled_run uuid;
            schedule_key text;
            scheduled_count integer := 0;
        BEGIN
            IF p_limit < 1 OR p_limit > 500 THEN
                RAISE EXCEPTION 'continuous eval limit must be between 1 and 500';
            END IF;
            IF p_release IS NULL OR length(p_release) < 1 OR length(p_release) > 64 THEN
                RAISE EXCEPTION 'release must contain 1 to 64 characters';
            END IF;

            FOR policy IN
                SELECT * FROM public.continuous_eval_policy
                WHERE next_run_at <= now()
                ORDER BY next_run_at, tenant_id, dataset_name
                FOR UPDATE SKIP LOCKED
                LIMIT p_limit
            LOOP
                SELECT id, content_sha256 INTO dataset
                FROM public.eval_dataset
                WHERE tenant_id = policy.tenant_id
                  AND name = policy.dataset_name
                ORDER BY created_at DESC
                LIMIT 1;

                IF dataset.id IS NULL THEN
                    -- A direct database purge may momentarily leave the policy
                    -- ahead of the dataset. Back off rather than hot-looping.
                    UPDATE public.continuous_eval_policy
                    SET next_run_at = now() + make_interval(secs => interval_seconds),
                        updated_at = now()
                    WHERE id = policy.id;
                    CONTINUE;
                END IF;

                schedule_key := 'continuous-eval:' || policy.id::text || ':' ||
                    extract(epoch from policy.next_run_at)::bigint::text;
                scheduled_run := NULL;
                INSERT INTO public.run
                    (tenant_id, workload, status, idempotency_key, requested_by,
                     input, max_attempts, release, priority, available_at)
                VALUES
                    (policy.tenant_id, 'eval', 'pending', schedule_key,
                     coalesce(policy.updated_by, policy.created_by),
                     jsonb_build_object(
                         'dataset', policy.dataset_name,
                         'content_sha256', dataset.content_sha256,
                         'top_k', policy.top_k,
                         'continuous', true
                     ),
                     3, p_release, -1, now())
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                RETURNING id INTO scheduled_run;

                IF scheduled_run IS NOT NULL THEN
                    INSERT INTO public.outbox
                        (tenant_id, run_id, workload, payload, trace_context)
                    VALUES
                        (policy.tenant_id, scheduled_run, 'eval',
                         jsonb_build_object(
                             'dataset', policy.dataset_name,
                             'content_sha256', dataset.content_sha256,
                             'top_k', policy.top_k,
                             'continuous', true
                         ), '{}'::jsonb);
                    scheduled_count := scheduled_count + 1;
                ELSE
                    SELECT id INTO scheduled_run FROM public.run
                    WHERE tenant_id = policy.tenant_id
                      AND idempotency_key = schedule_key;
                END IF;

                -- Do not replay every missed interval after an outage: one
                -- current observation is useful, a catch-up storm is not.
                UPDATE public.continuous_eval_policy
                SET last_run_id = scheduled_run,
                    last_scheduled_at = now(),
                    next_run_at = now() + make_interval(secs => interval_seconds),
                    updated_at = now()
                WHERE id = policy.id;
            END LOOP;
            RETURN scheduled_count;
        END
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "platform_schedule_due_continuous_evals(integer,text) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION "
        f"platform_schedule_due_continuous_evals(integer,text) TO {RELAY_ROLE}"
    )

    op.execute(
        """
        CREATE FUNCTION platform_continuous_eval_health()
        RETURNS jsonb
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH names AS (
                SELECT DISTINCT tenant_id, name FROM public.eval_dataset
            ), missing AS (
                SELECT count(*) AS n FROM names d
                LEFT JOIN public.continuous_eval_policy p
                  ON p.tenant_id = d.tenant_id AND p.dataset_name = d.name
                WHERE p.id IS NULL
            ), overdue AS (
                SELECT count(*) AS n FROM public.continuous_eval_policy
                WHERE next_run_at < now() - make_interval(secs => interval_seconds)
            )
            SELECT jsonb_build_object(
                'dataset_names', (SELECT count(*) FROM names),
                'missing_policies', (SELECT n FROM missing),
                'overdue_policies', (SELECT n FROM overdue),
                'ok', (SELECT n = 0 FROM missing) AND (SELECT n = 0 FROM overdue)
            )
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION platform_continuous_eval_health() FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION platform_continuous_eval_health() "
        f"TO {APP_ROLE}, {RELAY_ROLE}"
    )

    # Two small compatibility functions keep existing callers while removing
    # runtime use of owner_session(). Their arguments are bounded and they can
    # perform only the named purge.
    op.execute(
        """
        CREATE FUNCTION platform_purge_expired_sessions() RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE removed integer;
        BEGIN
            DELETE FROM public.session WHERE expires_at <= now();
            GET DIAGNOSTICS removed = ROW_COUNT;
            RETURN removed;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION platform_purge_unanswered_questions(p_days integer)
        RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE removed integer;
        BEGIN
            IF p_days < 1 OR p_days > 3650 THEN
                RAISE EXCEPTION 'gap retention must be between 1 and 3650 days';
            END IF;
            DELETE FROM public.unanswered_question
            WHERE seeded_into IS NULL
              AND last_asked_at < now() - make_interval(days => p_days);
            GET DIAGNOSTICS removed = ROW_COUNT;
            RETURN removed;
        END
        $$
        """
    )

    # Returns category counts for metrics and evidence. Audit deletion advances
    # a durable anchor first; a crash cannot commit one without the other.
    op.execute(
        """
        CREATE FUNCTION platform_enforce_retention(
            p_gap_days integer,
            p_run_days integer,
            p_eval_days integer,
            p_usage_days integer,
            p_audit_days integer,
            p_audit_batch integer DEFAULT 100
        ) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            removed_sessions integer := 0;
            removed_gaps integer := 0;
            removed_runs integer := 0;
            removed_evals integer := 0;
            removed_datasets integer := 0;
            removed_usage integer := 0;
            removed_reservations integer := 0;
            removed_audit integer := 0;
            removed_batch integer := 0;
            boundary record;
        BEGIN
            IF p_gap_days NOT BETWEEN 1 AND 3650
               OR p_run_days NOT BETWEEN 1 AND 3650
               OR p_eval_days NOT BETWEEN 1 AND 3650
               OR p_usage_days NOT BETWEEN 1 AND 3650
               OR p_audit_days NOT BETWEEN 1 AND 3650
               OR p_audit_batch NOT BETWEEN 1 AND 1000 THEN
                RAISE EXCEPTION 'retention parameters are outside safe bounds';
            END IF;

            removed_sessions := public.platform_purge_expired_sessions();
            removed_gaps := public.platform_purge_unanswered_questions(p_gap_days);

            DELETE FROM public.run
            WHERE status IN ('succeeded', 'failed', 'cancelled')
              AND finished_at < now() - make_interval(days => p_run_days);
            GET DIAGNOSTICS removed_runs = ROW_COUNT;

            DELETE FROM public.eval_run r
            WHERE r.finished_at < now() - make_interval(days => p_eval_days)
              AND NOT EXISTS (
                  SELECT 1 FROM public.eval_baseline b WHERE b.eval_run_id = r.id
              );
            GET DIAGNOSTICS removed_evals = ROW_COUNT;

            DELETE FROM public.eval_dataset d
            WHERE d.created_at < now() - make_interval(days => p_eval_days)
              AND NOT EXISTS (
                  SELECT 1 FROM public.eval_run r WHERE r.dataset_id = d.id
              )
              AND d.id <> (
                  SELECT newest.id FROM public.eval_dataset newest
                  WHERE newest.tenant_id = d.tenant_id AND newest.name = d.name
                  ORDER BY newest.created_at DESC LIMIT 1
              );
            GET DIAGNOSTICS removed_datasets = ROW_COUNT;

            DELETE FROM public.llm_usage
            WHERE at < now() - make_interval(days => p_usage_days);
            GET DIAGNOSTICS removed_usage = ROW_COUNT;

            DELETE FROM public.budget_reservation
            WHERE status IN ('settled', 'released', 'expired')
              AND created_at < now() - make_interval(days => p_run_days);
            GET DIAGNOSTICS removed_reservations = ROW_COUNT;

            FOR boundary IN
                WITH candidates AS (
                    SELECT id, tenant_id, hash,
                           row_number() OVER (PARTITION BY tenant_id ORDER BY id) AS pos
                    FROM public.audit_event
                    WHERE at < now() - make_interval(days => p_audit_days)
                )
                SELECT DISTINCT ON (tenant_id) id, tenant_id, hash, pos
                FROM candidates
                WHERE pos <= p_audit_batch
                ORDER BY tenant_id, id DESC
            LOOP
                INSERT INTO public.audit_chain_anchor
                    (tenant_id, through_event_id, through_hash, events_anchored,
                     reason, anchored_at)
                VALUES
                    (boundary.tenant_id, boundary.id, boundary.hash, boundary.pos,
                     'scheduled retention after ' || p_audit_days || ' days', now())
                ON CONFLICT (tenant_id) DO UPDATE
                SET through_event_id = EXCLUDED.through_event_id,
                    through_hash = EXCLUDED.through_hash,
                    events_anchored = public.audit_chain_anchor.events_anchored
                                      + EXCLUDED.events_anchored,
                    reason = EXCLUDED.reason,
                    anchored_at = now();

                PERFORM set_config(
                    'app.audit_purge_reason',
                    'scheduled retention anchored through event ' || boundary.id,
                    true
                );
                DELETE FROM public.audit_event
                WHERE tenant_id = boundary.tenant_id AND id <= boundary.id;
                GET DIAGNOSTICS removed_batch = ROW_COUNT;
                removed_audit := removed_audit + removed_batch;
            END LOOP;

            RETURN jsonb_build_object(
                'sessions', removed_sessions,
                'gaps', removed_gaps,
                'runs', removed_runs,
                'eval_runs', removed_evals,
                'eval_datasets', removed_datasets,
                'usage', removed_usage,
                'budget_reservations', removed_reservations,
                'audit_events', removed_audit
            );
        END
        $$
        """
    )

    for signature in (
        "platform_purge_expired_sessions()",
        "platform_purge_unanswered_questions(integer)",
        "platform_enforce_retention(integer,integer,integer,integer,integer,integer)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {RELAY_ROLE}")


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "platform_enforce_retention(integer,integer,integer,integer,integer,integer)"
    )
    op.execute("DROP FUNCTION IF EXISTS platform_purge_unanswered_questions(integer)")
    op.execute("DROP FUNCTION IF EXISTS platform_purge_expired_sessions()")
    op.execute("DROP FUNCTION IF EXISTS platform_continuous_eval_health()")
    op.execute(
        "DROP FUNCTION IF EXISTS platform_schedule_due_continuous_evals(integer,text)"
    )
    op.execute("DROP POLICY IF EXISTS audit_chain_anchor_tenant_isolation ON audit_chain_anchor")
    op.drop_table("audit_chain_anchor")
    op.execute(
        "DROP POLICY IF EXISTS continuous_eval_policy_tenant_isolation "
        "ON continuous_eval_policy"
    )
    op.drop_index("continuous_eval_due_idx", table_name="continuous_eval_policy")
    op.drop_table("continuous_eval_policy")
