"""Score a golden set against the live corpus, and report the gate's verdict.

The gate itself is not here. ``platform_core/gates/`` already holds the whole
thing — dataset versioning, the runner, the comparative promotion decision — and
has since Phase 4, with ten property tests and a mutation control behind it. What
it never had was a caller: nothing outside ``tests/properties/test_eval_gates.py``
imported it. The most valuable discipline in the build was reachable only from
pytest.

So this workload adds no scoring logic and no second notion of a dataset. It is
the platform half:

* the run is queued through the outbox, leased, retried and swept like any other
  work, instead of blocking an HTTP request for the minutes an eval takes;
* embedding goes through the instrumented client, so a hundred questions are
  attributed and **budget-checked** — ``task="eval"`` is not in
  ``INTERACTIVE_TASKS``, so an unreadable ledger fails the run closed;
* the verdict is *recorded*, not acted on.

## Deciding and promoting are separate acts

:func:`platform_core.gates.promotion.evaluate` returns a decision; ``promote``
moves the baseline and requires ``release:promote``. This workload calls only the
first. A worker that promoted its own candidate would be the release equivalent
of approving your own schema — and the same shape as the Azure build's ingest
path, which decides that a build is good by having finished.

The decision is stored on the run so the person who *does* hold the capability
sees the reasons that were computed at the time, rather than recomputing them
against a baseline that may since have moved.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from platform_core.corpus import builds
from platform_core.db.engine import tenant_session
from platform_core.gates import datasets, promotion, runner
from platform_core.gates import judge as judging
from platform_core.gates import labels as item_labels
from platform_core.identity.principal import RequestContext
from platform_core.ports.llm import ChatRequest

logger = logging.getLogger("platform.workloads.eval")

WORKLOAD = "eval"

DEFAULT_TOP_K = 5


def _chunk_texts(ctx: RequestContext, collection: str,
                 chunk_ids: list[str]) -> dict[str, str]:
    """Chunk text by canonical id, from the build being served.

    Used for two different things and deliberately shared: the answerer needs the
    retrieved text to answer from, and the metrics need the *expected* evidence
    text to measure faithfulness against. Reading them through one live-build
    lookup means the two can never disagree about which corpus they are talking
    about.
    """
    if not chunk_ids:
        return {}
    with tenant_session(ctx.tenant) as s:
        build_version = builds.live_version_or_none(ctx, collection, session=s)
        if build_version is None:
            return {}
        rows = s.execute(
            text(
                "SELECT canonical_id, text FROM chunk "
                "WHERE collection = :c AND build_version = :v "
                "  AND canonical_id = ANY(:ids)"
            ),
            {"c": collection, "v": build_version, "ids": list(set(chunk_ids))},
        ).all()
    return {r.canonical_id: r.text for r in rows}


def _make_answerer(ctx: RequestContext, llm, model: str):
    """The system under test: answer from the retrieved chunks, and only those.

    Reuses ``chat.SYSTEM_PROMPT`` and the same ``[c_xxxx] text`` context block
    rather than restating them. The prompt *is* the behaviour being measured — a
    second copy here would drift from the one users hit, and the eval would
    slowly start grading a system nobody runs.

    It does not call ``chat.answer``, which retrieves for itself and opens a
    session. The eval has already retrieved, and scoring recall against one
    retrieval while grading an answer produced by another would make the two
    numbers describe different runs.
    """
    from workloads.chat.service import SYSTEM_PROMPT

    def answer(inner_ctx: RequestContext, collection: str, item, retrieved: list[str]) -> str:
        if not retrieved:
            # The same refusal the chat surface gives. Letting the model answer
            # from prior knowledge here would score the model, not the platform.
            return (
                "I don't have any indexed sources for that. Nothing in this "
                "collection matches the question."
            )
        texts = _chunk_texts(inner_ctx, collection, retrieved)
        context_block = "\n\n".join(
            f"[{cid}] {texts[cid]}" for cid in retrieved if cid in texts
        )
        response = llm.chat(
            inner_ctx,
            ChatRequest(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Sources:\n{context_block}\n\nQuestion: {item.question}",
                    },
                ],
                temperature=0,
                max_tokens=400,
                # Identical question and identical context is genuinely the same
                # call, and an eval re-run over an unchanged corpus should not
                # pay twice.
                cacheable=True,
            ),
        )
        return response.content

    return answer


def run(ctx: RequestContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Score one dataset version. Safe to call again on the same ``ctx.run_id``.

    Not idempotent in the sense of producing one row: every execution writes a
    new ``eval_run``, and that is the point — a run is an observation, and
    collapsing two observations of the same dataset would destroy exactly the
    trend the history exists to show. Re-delivery is prevented by the lease, not
    by deduplicating the measurement.
    """
    dataset_name = str(payload["dataset"])
    content_sha = payload.get("content_sha256") or None
    top_k = int(payload.get("top_k") or DEFAULT_TOP_K)

    dataset = datasets.load(ctx, name=dataset_name, content_sha256=content_sha)
    if dataset is None:
        # A missing dataset is a permanent failure, not a transient one: retrying
        # will not make it appear, and the run should stop rather than occupy the
        # queue until it gives up.
        raise ValueError(
            f"no dataset named {dataset_name!r}"
            + (f" at version {content_sha[:12]}…" if content_sha else "")
        )

    from platform_core.observability.llm import build_client

    llm = build_client()
    # `task="eval"` rather than the workload name: the ledger's per-task policy
    # branches on it, and eval is a background path — hundreds of embedding calls
    # with nobody waiting. An unreadable ledger fails this closed.
    scoped = ctx.child(step="score", task="eval")

    def embed(texts: list[str]) -> list[list[float]]:
        return llm.embed(scoped, texts)

    from platform_core.settings import get_settings

    settings = get_settings()
    labels = item_labels.for_dataset(ctx, dataset_name)

    # Three separate models, and the separation is the measurement. The answerer
    # is what users hit; the judge grades it and must not be the same model, or
    # it marks its own homework and the numbers are flattering in a direction no
    # report reveals. `Settings.check_coherence` refuses that configuration at
    # startup, so this reads them rather than choosing them.
    answerer = _make_answerer(scoped, llm, settings.llm_model_cheap)

    def grade(item, actual: str, retrieved: list[str]):
        return judging.verdict(
            scoped, item, actual_answer=actual, retrieved=retrieved, llm=llm
        )

    def evidence(chunk_ids: list[str]) -> dict[str, str]:
        return _chunk_texts(scoped, dataset.collection, chunk_ids)

    completed = runner.run(
        scoped,
        dataset,
        retrieve=runner.pgvector_retriever(embed),
        answer=answerer,
        judge=grade,
        evidence=evidence,
        labels=labels,
        top_k=top_k,
    )

    # Evaluate, do not promote. See the module docstring.
    decision = promotion.evaluate(ctx, completed, dataset_name=dataset_name)

    # Where the failures point, counted. Twelve failures all naming
    # `kg:entity_type` is a different morning's work from twelve naming
    # anything, and that is only visible as a distribution.
    surfaces: dict[str, int] = {}
    for outcome in completed.outcomes:
        if outcome.passed is False and outcome.fix_surface:
            surfaces[outcome.fix_surface] = surfaces.get(outcome.fix_surface, 0) + 1

    result = {
        **completed.summary(),
        "dataset": dataset_name,
        "collection": dataset.collection,
        "fix_surfaces": surfaces,
        "review": item_labels.summarise(dataset.items, labels),
        "gate": {
            "would_promote": decision.promoted,
            "reasons": decision.reasons,
            "deltas": decision.deltas,
            "baseline_run_id": (decision.baseline or {}).get("run_id"),
        },
    }

    if not decision.promoted:
        from platform_core.observability.telemetry import record_eval_gate

        record_eval_gate("blocked")
        logger.warning(
            "eval %s on %s would NOT promote:\n%s",
            completed.id, dataset_name, decision.explain(),
        )
    else:
        from platform_core.observability.telemetry import record_eval_gate

        record_eval_gate("passed")
        logger.info("eval %s on %s clears the gate", completed.id, dataset_name)
    return result
