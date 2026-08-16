"use client";

import { useCallback, useState } from "react";
import {
  draftEvalAnswers,
  fetchEvalDataset,
  reviewEvalItem,
  type EvalDatasetDetail,
} from "@/lib/api";
import { ErrorNote, Spinner } from "./Panel";

/**
 * Reviewing a golden set: read the drafted answers, edit them, attest to them.
 *
 * The whole point of the screen is the distinction between an answer a person
 * read and one they clicked past. An eval set nobody has read is not ground
 * truth, and a pass rate measured against it is a number about the annotator
 * rather than about the platform — so `accepted_unedited` is shown at the top
 * rather than buried in a summary nobody expands.
 *
 * Editing an answer mints a new dataset version; confirming does not. That is
 * stated in the UI because it is surprising and load-bearing: the version is
 * what a baseline is comparable against, so a reviewer needs to know which of
 * their two actions moved it.
 */
export function EvalReview({
  name,
  canWrite,
  onChanged,
}: {
  name: string;
  canWrite: boolean;
  onChanged: () => void;
}) {
  const [data, setData] = useState<EvalDatasetDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    setError(null);
    try {
      setData(await fetchEvalDataset(name));
      setDrafts({});
    } catch (err) {
      setError(err);
    }
  }, [name]);

  // Kicked off once on mount by the parent rendering this component.
  if (data === null && error === null && !busy) {
    void load();
  }

  const act = async (fn: () => Promise<unknown>, message: string) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setNote(message);
      await load();
      onChanged();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  if (error) return <ErrorNote error={error} />;
  if (!data) return <Spinner label="Loading the set…" />;

  const r = data.review;
  const unread = r.accepted_unedited;

  return (
    <div className="mt-3 rounded-xl border border-cream-300 bg-cream-50/60 p-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-500">
        <span className="font-medium text-ink-700">{data.name}</span>
        <span className="font-mono">{data.content_sha256.slice(0, 12)}…</span>
        <span>{r.total} items</span>
        <span>{r.with_expected_answer} with an answer</span>
        <span>{r.confirmed} confirmed</span>
        {r.requires_kg_hop ? <span>{r.requires_kg_hop} need a KG hop</span> : null}
        {r.unusable ? <span className="text-warn">{r.unusable} unusable</span> : null}
        {r.annotator_models.length ? (
          <span className="font-mono">by {r.annotator_models.join(", ")}</span>
        ) : null}
      </div>

      {/* The signal that separates review from clicking through. */}
      {unread > 0 && (
        <p className="mt-2 rounded-lg border border-warn/30 bg-warn/5 px-2.5 py-1.5 text-[12px] text-ink-700">
          <strong>{unread}</strong> confirmed without a single edit — those are
          the annotator&apos;s answers, not reviewed ground truth. A pass rate
          measured against them says how consistent two models are, not whether
          the platform is right. Report them as such, or read them.
        </p>
      )}

      {canWrite ? (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy || r.with_expected_answer >= r.total}
            title={
              r.with_expected_answer >= r.total
                ? "Every item already has an expected answer."
                : "Drafts references from each item's cited evidence — never from what retrieval returns. Spends budget."
            }
            onClick={() =>
              void act(
                async () => {
                  const out = await draftEvalAnswers(name);
                  setNote(
                    `Drafted ${out.drafted} with ${out.model}` +
                      (out.skipped_no_evidence
                        ? `; ${out.skipped_no_evidence} had no readable evidence`
                        : ""),
                  );
                },
                "Drafted.",
              )
            }
            className="btn-ghost px-2 py-1 text-[12px]"
          >
            Draft missing answers
          </button>
          <span className="text-[11px] text-ink-400">
            Editing an answer creates a new dataset version; confirming does not.
          </span>
        </div>
      ) : null}

      {note ? (
        <p className="mt-2 text-[12px] text-ink-500">{note}</p>
      ) : null}

      <ul className="mt-2 space-y-2">
        {data.items.map((item) => {
          const label = data.labels[item.id];
          const source = label?.answer_source ?? "empty";
          const unusable = label?.unusable_reason ?? "";
          const value = drafts[item.id] ?? item.expected_answer;
          return (
            <li
              key={item.id}
              className={`rounded-lg border p-2 ${
                unusable ? "border-danger/30 bg-danger/5" : "border-cream-300 bg-white"
              }`}
            >
              <div className="text-[12.5px] text-ink-800">{item.question}</div>
              <div className="mt-0.5 flex flex-wrap gap-x-2 text-[11px] text-ink-400">
                <span className="font-mono">{item.id}</span>
                <span>
                  {item.must_cite.length
                    ? `${item.must_cite.length} evidence chunk${item.must_cite.length === 1 ? "" : "s"}`
                    : "no evidence — recall is not scoreable for this item"}
                </span>
                <span
                  className={
                    source === "llm_drafted" && label?.confirmed ? "text-warn" : ""
                  }
                >
                  {source}
                </span>
                {label?.annotator_model ? (
                  <span className="font-mono">{label.annotator_model}</span>
                ) : null}
              </div>

              {unusable ? (
                <p className="mt-1 text-[12px] text-danger">
                  Flagged unusable: {unusable}
                </p>
              ) : (
                <textarea
                  rows={2}
                  value={value}
                  readOnly={!canWrite}
                  placeholder="Expected answer — edit it, then it counts as reviewed."
                  onChange={(e) =>
                    setDrafts({ ...drafts, [item.id]: e.target.value })
                  }
                  className="mt-1 w-full rounded-md border border-cream-300 p-1.5 text-[12.5px] text-ink-800 focus:border-copper-400 focus:outline-none"
                />
              )}

              {canWrite ? (
                <div className="mt-1 flex flex-wrap items-center gap-3 text-[12px] text-ink-600">
                  <button
                    type="button"
                    disabled={busy || value === item.expected_answer}
                    onClick={() =>
                      void act(
                        () =>
                          reviewEvalItem(name, item.id, { expected_answer: value }),
                        "Answer saved — this created a new dataset version.",
                      )
                    }
                    className="btn-primary px-2 py-0.5 text-[11px]"
                  >
                    Save answer
                  </button>
                  <label className="flex items-center gap-1">
                    <input
                      type="checkbox"
                      checked={!!label?.confirmed}
                      disabled={busy}
                      onChange={(e) =>
                        void act(
                          () =>
                            reviewEvalItem(name, item.id, {
                              confirmed: e.target.checked,
                            }),
                          e.target.checked ? "Confirmed." : "Confirmation cleared.",
                        )
                      }
                    />
                    confirmed
                  </label>
                  <label
                    className="flex items-center gap-1"
                    title="Marks the items whose answer needs a graph traversal, so a taxonomy fix can be measured on the slice it should move."
                  >
                    <input
                      type="checkbox"
                      checked={!!label?.requires_kg_hop}
                      disabled={busy}
                      onChange={(e) =>
                        void act(
                          () =>
                            reviewEvalItem(name, item.id, {
                              requires_kg_hop: e.target.checked,
                            }),
                          "Label saved.",
                        )
                      }
                    />
                    needs a KG hop
                  </label>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void act(
                        () =>
                          reviewEvalItem(name, item.id, {
                            unusable_reason: unusable
                              ? ""
                              : "reviewer marked the evidence unusable",
                          }),
                        unusable
                          ? "Un-flagged; it will be scored again."
                          : "Flagged — excluded from runs and counted.",
                      )
                    }
                    className="underline"
                  >
                    {unusable ? "un-flag" : "flag unusable"}
                  </button>
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>

      <p className="mt-2 text-[11px] leading-relaxed text-ink-400">
        Results are not shown here — running the set needs a live build, and a
        run lives with the build it scored. Use <em>Run</em> on the row above.
      </p>
    </div>
  );
}
