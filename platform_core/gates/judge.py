"""LLM-as-judge: does the answer convey what the reference says, and if not, where to look.

## The judge must not be the model it is grading

``llm_model_judge`` defaults to a different model from ``llm_model_cheap``, which
is what answers, and :meth:`Settings.check_coherence` refuses a configuration
where they are equal. A judge sharing a model with the answerer marks its own
homework — and nothing about the resulting numbers looks wrong. They are simply
flattering, permanently, in a direction no report reveals. That is why this is a
startup refusal rather than a comment.

## Grade loosely, and say why

The corpus discusses the same fact in several chunks, so a correct answer often
cites different evidence from the reference. Failing on that measures agreement
with the annotator's chunk selection rather than correctness, and produces a
score that falls when retrieval gets *better* at finding alternatives.

So the rubric fails an answer only when it misses every key fact, contradicts
one, or fabricates an absence — "the document does not list X" when the evidence
plausibly contains X. That last one is worth naming: a confident denial reads as
careful behaviour and is the failure most likely to be believed.

## A verdict points at a surface

``fix_surface`` is a closed enum, not free text, because the value of a failing
eval is the next action. "Wrong" tells a reviewer to go looking; "the entity is
missing from the instance table" tells them where. A closed set also means the
distribution across a run is countable — twelve failures all pointing at
``kg:entity_type`` is a different morning's work from twelve pointing anywhere.

## Response format degrades rather than failing

The reference deployment learned this expensively: ``json_schema`` is not
accepted by every model, the resulting 400 was caught by a blanket handler and
reported as "judge unavailable", and **every item scored as a failure while
nothing was wrong with the answers**. The judge deliberately runs on a different
model from everything else, so the format has to bend to the model rather than
the model being chosen to suit the format. Three formats are attempted in order
and the prompt states the shape regardless.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from platform_core.correctness.cancellation import RunCancelled, cancellation_point
from platform_core.gates.datasets import EvalItem
from platform_core.identity.principal import RequestContext
from platform_core.ports.llm import ChatRequest
from platform_core.settings import get_settings

logger = logging.getLogger("platform.gates.judge")


class FixSurface(StrEnum):
    """Where a reviewer should look. Small and actionable on purpose."""

    ANSWER_PROMPT = "prompts:answer"
    RETRIEVAL = "retrieval:top_k"
    KG_INSTANCE_TABLE = "kg:instance_table"
    KG_ENTITY_TYPE = "kg:entity_type"
    CORPUS_GAP = "corpus:gap"
    EXPECTED_ANSWER = "eval:expected_answer"
    NONE = "none"

    def explain(self) -> str:
        return _EXPLAIN[self]


_EXPLAIN: dict[FixSurface, str] = {
    FixSurface.ANSWER_PROMPT:
        "The answer prompt — the evidence was there and the answer did not use it.",
    FixSurface.RETRIEVAL:
        "Retrieval — the answering chunk exists in the corpus and did not come back. "
        "Raise top_k, or check the collection's live build.",
    FixSurface.KG_INSTANCE_TABLE:
        "The instance table — an entity the graph should know is not in it. "
        "Edit the taxonomy and retype, then re-publish.",
    FixSurface.KG_ENTITY_TYPE:
        "Schema entity types — the type is missing, so its relations are discarded.",
    FixSurface.CORPUS_GAP:
        "The corpus — nothing retrieved could answer this. The document is missing, "
        "not the retrieval.",
    FixSurface.EXPECTED_ANSWER:
        "The expected answer itself — the reference looks wrong or unanswerable, "
        "which is a finding about the eval set rather than the platform.",
    FixSurface.NONE: "Nothing to fix; the item passed.",
}


@dataclass(frozen=True, slots=True)
class Verdict:
    passed: bool
    reason: str
    fix_surface: FixSurface = FixSurface.NONE
    # True when the judge itself could not be reached or parsed. Distinguished
    # from a genuine failure because they demand opposite responses: one is a
    # regression in the platform, the other is a broken measurement — and a run
    # where the judge was down must not read as a run where the answers were bad.
    judge_unavailable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "fix_surface": self.fix_surface.value,
            "judge_unavailable": self.judge_unavailable,
        }


_SYSTEM = """\
You grade whether an answer produced by a retrieval system conveys the same key \
facts as a reference answer, and pick where a failure should be fixed.

You are given QUESTION, EXPECTED_ANSWER, ACTUAL_ANSWER, MUST_CITE (the chunk ids \
the reference was written from — informational only), and RETRIEVED_CHUNK_IDS.

Grading rules:

1. Grade loosely on content. The corpus states the same fact in several places, \
so the system may answer correctly from chunks other than MUST_CITE. That is not \
a failure.
2. passed=true when ACTUAL_ANSWER conveys the same key facts as EXPECTED_ANSWER. \
Different phrasing, extra context and different evidence are all fine. Allow \
partial credit when most key facts are covered.
3. passed=false only when the answer misses all the key facts, contradicts one, \
or claims the source does not contain something the evidence plausibly contains.
4. Never fail an answer merely for citing different chunks, or for citing none.

Choose exactly one fix_surface:
  prompts:answer        the evidence was retrieved and the answer ignored it
  retrieval:top_k       the answering content exists but was not retrieved
  kg:instance_table     an entity the graph should know is missing
  kg:entity_type        an entity type is missing from the schema
  corpus:gap            nothing in the corpus could answer this
  eval:expected_answer  the reference itself looks wrong or unanswerable
  none                  the item passed
"""

_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "verdict",
        "schema": {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "reason": {"type": "string"},
                "fix_surface": {
                    "type": "string",
                    "enum": [s.value for s in FixSurface],
                },
            },
            "required": ["passed", "reason", "fix_surface"],
            "additionalProperties": False,
        },
    },
}

_SHAPE_HINT = (
    '\n\nRespond with only a json object, no prose and no code fence:\n'
    '{"passed": true|false, "reason": "<one short sentence>", '
    '"fix_surface": "<one of the values above>"}'
)

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def _parse(raw: str) -> Verdict | None:
    text = _FENCE.sub("", raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "passed" not in data:
        return None
    try:
        surface = FixSurface(str(data.get("fix_surface") or "none"))
    except ValueError:
        # An invented surface is not worth discarding the verdict over, but it
        # must not be recorded as though the judge had chosen one.
        surface = FixSurface.NONE
    return Verdict(
        passed=bool(data["passed"]),
        reason=str(data.get("reason") or "")[:1000],
        fix_surface=surface,
    )


def verdict(
    ctx: RequestContext,
    item: EvalItem,
    *,
    actual_answer: str,
    retrieved: list[str],
    llm,
    model: str | None = None,
) -> Verdict:
    """Grade one answer. Never raises.

    An unreachable or unparseable judge returns ``judge_unavailable=True`` with
    ``passed=False``, and the runner counts those separately rather than folding
    them into the pass rate. A measurement that failed to happen is not a
    measurement of failure.
    """
    settings = get_settings()
    chosen = model or settings.llm_model_judge

    if not (item.expected_answer or "").strip():
        # Nothing to grade against. Reported rather than passed: an item with no
        # reference cannot contribute to a pass rate, and silently passing it
        # would inflate every future comparison.
        return Verdict(
            passed=False,
            reason="no expected answer on this item — draft or write one first",
            fix_surface=FixSurface.EXPECTED_ANSWER,
            judge_unavailable=True,
        )

    user = (
        f"QUESTION:\n{item.question}\n\n"
        f"EXPECTED_ANSWER:\n{item.expected_answer}\n\n"
        f"ACTUAL_ANSWER:\n{actual_answer or '(the system produced no answer)'}\n\n"
        f"MUST_CITE:\n{', '.join(item.must_cite) or '(none)'}\n\n"
        f"RETRIEVED_CHUNK_IDS:\n{', '.join(retrieved[:20]) or '(none)'}"
    )

    last_error: Exception | None = None
    for response_format in (_SCHEMA, {"type": "json_object"}, None):
        cancellation_point(ctx)
        try:
            response = llm.chat(
                ctx,
                ChatRequest(
                    model=chosen,
                    messages=[
                        {"role": "system", "content": _SYSTEM + _SHAPE_HINT},
                        {"role": "user", "content": user},
                    ],
                    temperature=0,
                    max_tokens=300,
                    response_format=response_format,
                ),
            )
        except RunCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — every provider failure looks different
            last_error = exc
            logger.debug("judge call failed with response_format=%s: %s",
                         response_format, exc)
            continue

        parsed = _parse(response.content)
        if parsed is not None:
            return parsed
        logger.debug("judge returned unparseable content with response_format=%s",
                     response_format)

    logger.warning(
        "judge %s unavailable for item %s: %s", chosen, item.id, last_error
    )
    return Verdict(
        passed=False,
        reason=(
            f"judge unavailable: "
            f"{type(last_error).__name__ if last_error else 'unparseable output'}"
        ),
        fix_surface=FixSurface.NONE,
        judge_unavailable=True,
    )
