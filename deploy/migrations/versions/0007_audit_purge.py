"""audit erasure: deliberate, privileged, and impossible by accident

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-12

`0006` made `audit_event` append-only with a trigger that rejects UPDATE and
DELETE. Correct for tamper-evidence, and it made deleting a tenant **fail
outright**: `tenant_id` cascades, the cascade issues a DELETE, and the trigger
refuses it. Found immediately — the test-suite cleanup could no longer remove a
tenant.

That is not a test artifact. It is the real tension between two things that are
both true:

* an audit log that can be rewritten proves nothing, so deletion must be hard;
* erasure obligations are real, so deletion must be *possible*.

Resolved by making erasure explicit rather than either impossible or casual:

**UPDATE is refused unconditionally.** There is no legitimate reason to alter a
recorded event. Correcting the record means appending a correction.

**DELETE is refused unless the session declares a purge.** A caller must set
`app.audit_purge_reason`, which is recorded in the server log by the trigger.
Combined with the grants, this is narrow: the application role holds no DELETE
on `audit_event` at all, so the declaration only unlocks anything for the owner
role — which is migrations and deliberate operator action.

The GUC is a *declaration of intent*, not a privilege. It cannot grant DELETE to
a role that lacks it. That distinction is what makes this different from the
mistake reverted in `0003`, where a session variable was the sole thing standing
between the app role and every tenant's data.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_event_append_only()
        RETURNS TRIGGER AS $$
        DECLARE
            purge_reason text := NULLIF(current_setting('app.audit_purge_reason', true), '');
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION
                    'audit_event is append-only: UPDATE is never permitted. '
                    'Append a correcting event instead of altering the record.'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;

            IF purge_reason IS NULL THEN
                RAISE EXCEPTION
                    'audit_event is append-only: DELETE requires an explicit purge. '
                    'Set app.audit_purge_reason to record why the record is being erased.'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;

            -- Erasure is legitimate but must never be quiet. The reason lands in
            -- the server log, where it survives the row it is deleting.
            RAISE WARNING 'audit_event purge: id=% tenant=% reason=%',
                OLD.id, OLD.tenant_id, purge_reason;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_event_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'audit_event is append-only: % is not permitted', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql
        """
    )
