"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Cap,
  can,
  curateQuery,
  fetchCandidateQueries,
  seedEvalSet,
  type CandidateQuery,
  type Me,
  type OnboardingSession,
} from "@/lib/api";
import { ErrorNote, Spinner } from "./Panel";

/**
 * The questions this domain must answer, proposed from its own corpus.
 *
 * Before this, eval sets were authored by hand — which meant writing the
 * evidence chunk ids by hand too, and an id the retriever cannot emit scores a
 * permanent miss indistinguishable from a real retrieval failure. These come
 * with canonical ids attached because the drafter reads the same live build the
 * retriever queries.
 *
 * Nothing is approved by default, and only approved questions seed. A review
 * step that defaults to "yes" is decorative, and the whole value of seeding
 * rather than generating is that a person said these are the questions that
 * matter.
 */
export function CandidateQueries({
  session,
  me,
  onSeeded,
}: {
  session: OnboardingSession;
  me: Me;
  onSeeded: () => void;
}) {
  const [queries, setQueries] = useState<CandidateQuery[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});

  const mayCurate =
    can(me, Cap.SCHEMA_AUTHOR) && session.status !== "published";
  const maySeed = can(me, Cap.RELEASE_PROMOTE);

  const load = useCallback(async () => {
    try {
      const out = await fetchCandidateQueries(session.id);
      setQueries(out.queries);
      setEdits({});
    } catch (err) {
      setError(err);
    }
  }, [session.id]);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (fn: () => Promise<unknown>, message: string) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setNote(message);
      await load();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const proposed = (session.stats as Record<string, any>)?.candidate_queries;
  if (queries !== null && queries.length === 0) {
    // Zero and "not attempted" look identical from the outside and need
    // different responses, so the count is stated rather than the section hidden.
    return (
      <details className="mt-3">
        <summary className="cursor-pointer text-[12px] font-medium text-ink-600">
          Candidate questions
        </summary>
        <p className="mt-1.5 rounded-lg border border-cream-300 bg-cream-100/60 px-2.5 py-2 text-[12px] text-ink-500">
          None were proposed{proposed === 0 ? " (0 recorded on this draft)" : ""}.
          The sampler skips chunks under 300 characters, so a corpus of short
          chunks yields nothing — that is a property of the corpus, not a
          failure. An eval set can still be written by hand.
        </p>
      </details>
    );
  }

  const approved = (queries || []).filter((q) => q.approved).length;

  return (
    <details className="mt-3" open={!!queries?.length && approved === 0}>
      <summary className="cursor-pointer text-[12px] font-medium text-ink-600">
        Candidate questions{" "}
        {queries ? (
          <span className="text-ink-400">
            · {approved} of {queries.length} approved
          </span>
        ) : null}
      </summary>

      {error ? (
        <div className="mt-1.5">
          <ErrorNote error={error} />
        </div>
      ) : null}
      {!queries ? (
        <Spinner label="Loading questions…" />
      ) : (
        <>
          <p className="mt-1.5 text-[12px] leading-relaxed text-ink-500">
            Proposed from this collection, each carrying the id of the chunk it
            was drawn from. Approve the ones the domain genuinely has to answer,
            then seed a golden set — no model call, because the judgement was
            already made here.
          </p>

          {note ? (
            <p className="mt-1.5 text-[12px] text-ink-500">{note}</p>
          ) : null}

          <ul className="mt-2 space-y-1.5">
            {queries.map((q) => {
              const value = edits[q.id] ?? q.text;
              return (
                <li
                  key={q.id}
                  className={`rounded-lg border p-2 ${
                    q.approved
                      ? "border-ok/30 bg-ok/5"
                      : "border-cream-300 bg-white"
                  }`}
                >
                  <div className="flex items-start gap-2">
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={q.approved}
                      disabled={busy || !mayCurate}
                      onChange={(e) =>
                        void act(
                          () =>
                            curateQuery(session.id, q.id, {
                              approved: e.target.checked,
                            }),
                          e.target.checked ? "Approved." : "Removed.",
                        )
                      }
                    />
                    <textarea
                      rows={2}
                      value={value}
                      readOnly={!mayCurate}
                      onChange={(e) =>
                        setEdits({ ...edits, [q.id]: e.target.value })
                      }
                      className="w-full rounded-md border border-cream-300 px-2 py-1 text-[12.5px] text-ink-800 focus:border-copper-400 focus:outline-none"
                    />
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-400">
                    <span className="font-mono">
                      {q.evidence_chunk_ids.join(", ") || "no evidence"}
                    </span>
                    {q.source_file ? <span>{q.source_file}</span> : null}
                    {q.edited ? (
                      <span className="text-ink-600">edited by a human</span>
                    ) : null}
                    {!q.evidence_chunk_ids.length ? (
                      <span className="text-warn">
                        recall not scoreable for this item
                      </span>
                    ) : null}
                    {mayCurate && value !== q.text ? (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          void act(
                            () => curateQuery(session.id, q.id, { text: value }),
                            "Question saved.",
                          )
                        }
                        className="font-medium text-copper-600 underline"
                      >
                        save edit
                      </button>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>

          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={busy || !maySeed || approved === 0}
              title={
                !maySeed
                  ? `Requires ${Cap.RELEASE_PROMOTE} — a golden set is what the gate measures against.`
                  : approved === 0
                    ? "Approve at least one question first."
                    : "Creates a dataset from the approved questions. No model call."
              }
              onClick={() =>
                void act(async () => {
                  const out = await seedEvalSet(session.id);
                  setNote(
                    `Seeded "${out.dataset}" with ${out.items} items ` +
                      `(${out.items_scoreable} scoreable). ${out.note}`,
                  );
                  onSeeded();
                }, "Seeded.")
              }
              className="btn-primary px-3 py-1 text-[12px]"
            >
              Seed eval set ({approved})
            </button>
            <span className="text-[11px] text-ink-400">
              Expected answers stay blank — they are drafted and then read, in
              the Evaluation panel.
            </span>
          </div>
        </>
      )}
    </details>
  );
}
