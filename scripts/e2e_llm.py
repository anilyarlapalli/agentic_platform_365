"""Live check: one real OpenAI call, fully metered and attributed.

The property suite exercises the chain with fakes, which proves the wiring but
never touches a provider. This makes exactly one real call — on the cheapest
model, with a tiny prompt — and verifies that the ledger row, the cost and the
attribution all landed.

Costs about $0.00002. Run it after changing anything in the chain:

    .venv/bin/python -m scripts.e2e_llm
"""

from __future__ import annotations

import os
import uuid

from sqlalchemy import text


def main() -> int:
    os.environ.setdefault("SERVICE_ROLE", "test")
    os.environ.setdefault("ENVIRONMENT", "local")

    from platform_core.db.engine import owner_session, tenant_session
    from platform_core.identity.principal import (
        ActorType,
        Principal,
        RequestContext,
        Role,
        Tenant,
    )
    from platform_core.observability.ledger import ledger
    from platform_core.observability.llm import build_client
    from platform_core.ports.llm import ChatRequest

    slug = f"llm-{uuid.uuid4().hex[:8]}"
    with owner_session() as s:
        tenant_id = s.execute(
            text("INSERT INTO tenant (slug, name) VALUES (:s, 'LLM check') RETURNING id"),
            {"s": slug},
        ).scalar_one()
    tenant = Tenant(id=tenant_id, slug=slug)

    try:
        with tenant_session(tenant) as s:
            principal_id = s.execute(
                text(
                    "INSERT INTO principal (tenant_id, subject, roles) "
                    "VALUES (:t, 'llm@example.com', ARRAY['operator']) RETURNING id"
                ),
                {"t": tenant.id},
            ).scalar_one()

        ctx = RequestContext(
            principal=Principal(
                id=principal_id, tenant=tenant, subject="llm@example.com",
                roles=frozenset({Role.OPERATOR}), actor_type=ActorType.HUMAN,
            ),
            labels={"workload": "echo", "task": "chat"},
        )

        before = ledger.status(ctx)
        print(f"tokens today before: {before.tokens_today}")

        llm = build_client()
        response = llm.chat(
            ctx,
            ChatRequest(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "Reply with exactly: ok"}],
                max_tokens=5,
                temperature=0,
            ),
        )

        print(f"content:        {response.content!r}")
        print(f"model:          {response.model}")
        print(f"tokens:         in={response.usage.input_tokens} "
              f"out={response.usage.output_tokens} reported={response.usage.reported}")
        print(f"cost:           ${response.cost_usd:.8f}")
        print(f"attempts:       {response.attempts}")
        print(f"latency:        {response.latency_ms:.0f}ms")

        ledger.invalidate(tenant.id)
        after = ledger.status(ctx)
        print(f"tokens today after: {after.tokens_today}")

        with tenant_session(tenant) as s:
            row = s.execute(
                text(
                    "SELECT tenant_id, task, workload, model, input_tokens, "
                    "output_tokens, total_tokens, cost_usd, usage_reported "
                    "FROM llm_usage ORDER BY id DESC LIMIT 1"
                )
            ).one()

        print("\nledger row:")
        print(f"  tenant:  {row.tenant_id}")
        print(f"  task:    {row.task}   workload: {row.workload}")
        print(f"  model:   {row.model}")
        print(f"  tokens:  {row.input_tokens} + {row.output_tokens} = {row.total_tokens}")
        print(f"  cost:    ${float(row.cost_usd):.8f}")

        problems = []
        if row.task != "chat":
            problems.append(f"task recorded as {row.task!r}, not 'chat'")
        if row.total_tokens != response.usage.total:
            problems.append("ledger tokens disagree with the response")
        # The comparison that found the NUMERIC(12,6) truncation. Neither number
        # looked wrong alone; only holding them side by side showed the ledger
        # under-counting every sub-cent call.
        if float(row.cost_usd) != response.cost_usd:
            problems.append(
                f"ledger cost ${float(row.cost_usd):.12f} disagrees with the computed "
                f"cost ${response.cost_usd:.12f} — precision is being lost on the way in"
            )
        if not row.usage_reported:
            problems.append("provider reported no usage — cost is an estimate")
        if after.tokens_today <= before.tokens_today:
            problems.append("the budget window did not advance")
        if ledger.unattributed_spend() != 0:
            problems.append("unattributed spend is non-zero")

        if problems:
            print("\nFAILED:")
            for p in problems:
                print(f"  · {p}")
            return 1

        print("\nOK — one real call: metered, priced, attributed to a tenant, "
              "and counted against the budget window")
        return 0
    finally:
        with owner_session() as s:
            s.execute(
                text("SELECT set_config('app.audit_purge_reason', :w, true)"),
                {"w": "llm e2e check teardown"},
            )
            s.execute(text("DELETE FROM tenant WHERE slug = :s"), {"s": slug})


if __name__ == "__main__":
    raise SystemExit(main())
