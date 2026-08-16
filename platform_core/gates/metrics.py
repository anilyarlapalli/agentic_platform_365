"""Answer-quality metrics that cost nothing and never disagree with themselves.

Deterministic counterparts to the RAGAS-style measures. They are weaker than an
LLM judge on "is this answer actually right" and stronger on everything else:
free, reproducible to the digit, and computable on every item of every run
without a budget conversation. The judge grades a handful of items; these grade
all of them, and a divergence between the two is itself informative.

## What each one can and cannot tell you

``faithfulness``
    How much of the answer's vocabulary appears in the evidence it was given.
    Low means the answer contains material the context did not supply — the
    fingerprint of a model answering from prior knowledge. It cannot tell a
    correct inference from a fabrication, because both look like new words.

``answer_relevancy``
    Overlap with the question and the expected answer together. Catches an
    answer that is well-grounded and about something else.

``context_precision``
    Of what retrieval returned, how much was wanted. The counterpart to
    ``retrieval_recall``, which the runner already computes: recall asks whether
    the needed evidence came back, precision asks how much noise came with it.
    Recall alone is maximised by returning the whole corpus.

``citation_accuracy``
    Of the chunk ids the answer cited, how many were actually retrieved. A
    citation to something the retriever never returned is a fabricated one, and
    it is the cheapest hallucination to detect exactly.

None of these is a pass/fail. They are reported per item and averaged per run so
a regression has somewhere to show up between two judge verdicts.
"""

from __future__ import annotations

import re
from typing import Any

# `\w` plus hyphen, so `SA-400`, `F-051` and `gpt-4o` survive as single tokens.
# Splitting them would make every specification-heavy answer look unfaithful,
# which is precisely the corpus this platform is aimed at.
_TOKEN = re.compile(r"[\w\-]+")

# The citation form the chat surface emits: `[c_7119bc624364d1e3]`.
_CITATION = re.compile(r"\[(c_[0-9a-f]{6,32})\]")

# Words that overlap between any two English sentences and would inflate every
# score toward a floor that means nothing.
_STOP = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "in",
    "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was",
    "were", "when", "which", "with", "what", "how", "why", "does", "do",
})


def tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(text or "")} - _STOP


def _coverage(part: set[str], whole: set[str]) -> float:
    """How much of ``part`` is contained in ``whole``.

    Directional on purpose, where Jaccard would not be. An answer that is a
    faithful two-sentence extract from a page of evidence should score 1.0, and
    Jaccard would score it near zero for the crime of being concise.
    """
    if not part:
        return 0.0
    return round(len(part & whole) / len(part), 4)


def faithfulness(answer: str, evidence_texts: list[str]) -> float:
    """Share of the answer's content words that appear in its evidence."""
    if not (answer or "").strip():
        return 0.0
    supplied: set[str] = set()
    for text in evidence_texts:
        supplied |= tokens(text)
    return _coverage(tokens(answer), supplied)


def answer_relevancy(answer: str, question: str, expected_answer: str = "") -> float:
    """Share of the question-and-reference vocabulary the answer engages with.

    Measured over the reference rather than the answer, so padding cannot raise
    it: an answer that restates the corpus at length scores no better than one
    that addresses the question.
    """
    wanted = tokens(question) | tokens(expected_answer)
    if not wanted:
        return 0.0
    return _coverage(wanted, tokens(answer))


def context_precision(retrieved: list[str], must_cite: list[str]) -> float | None:
    """Of what came back, how much was wanted.

    ``None`` when the item declares no evidence — the same treatment the runner
    gives recall. Returning 0.0 would drag the average down for items that were
    never scoreable, which is the dilution the recall metric is careful to avoid.

    **Read it as a ceiling, not a grade.** ``must_cite`` is the *minimal*
    evidence an item needs, not an exhaustive relevance judgement over the
    corpus, so this is bounded above by ``len(must_cite) / len(retrieved)`` — one
    cited chunk at ``top_k=5`` can never score higher than 0.2 however good
    retrieval is. It is useful for comparing runs at a fixed ``top_k``, where a
    fall means real noise crept in, and misleading as an absolute number.
    """
    if not must_cite:
        return None
    if not retrieved:
        return 0.0
    wanted = set(must_cite)
    return round(len(wanted & set(retrieved)) / len(retrieved), 4)


def citations(answer: str) -> list[str]:
    """Chunk ids the answer claims to have used, in order, deduplicated."""
    seen: list[str] = []
    for match in _CITATION.findall(answer or ""):
        if match not in seen:
            seen.append(match)
    return seen


def citation_accuracy(answer: str, retrieved: list[str]) -> float | None:
    """Of the ids cited, how many were actually retrieved.

    ``None`` when the answer cites nothing. Scoring that 0.0 would punish an
    uncited-but-correct answer exactly as hard as a fabricated citation, and the
    two need different fixes.
    """
    cited = citations(answer)
    if not cited:
        return None
    available = set(retrieved)
    return round(sum(1 for c in cited if c in available) / len(cited), 4)


def score(
    *,
    answer: str,
    question: str,
    expected_answer: str,
    retrieved: list[str],
    evidence_texts: list[str],
    must_cite: list[str],
) -> dict[str, Any]:
    """Every metric for one item, in one call."""
    return {
        "faithfulness": faithfulness(answer, evidence_texts),
        "answer_relevancy": answer_relevancy(answer, question, expected_answer),
        "context_precision": context_precision(retrieved, must_cite),
        "citation_accuracy": citation_accuracy(answer, retrieved),
        "citations": citations(answer),
    }


def aggregate(per_item: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean of each metric over the items where it was measurable.

    Each metric averages over its own denominator, and the denominator is
    reported beside it. A mean over "the items that had one" and a mean over
    "all items, counting the others as zero" are different numbers, and only the
    first one means what a reader assumes.
    """
    out: dict[str, Any] = {}
    for name in ("faithfulness", "answer_relevancy", "context_precision",
                 "citation_accuracy"):
        values = [
            m[name] for m in per_item
            if isinstance(m.get(name), (int, float))
        ]
        out[name] = round(sum(values) / len(values), 4) if values else None
        out[f"{name}_n"] = len(values)
    return out
