"use client";

import { useState } from "react";
import {
  Cap,
  can,
  fetchEvalDatasets,
  fetchEvalRun,
  promoteEvalRun,
  startEvalRun,
  type EvalRunDetail,
  type Me,
} from "@/lib/api";
import {
  Denied,
  Empty,
  ErrorNote,
  Panel,
  Spinner,
  Table,
  useResource,
} from "./Panel";
import { EvalReview } from "./EvalReview";

/** A metric that may legitimately be null. */
function Metric({ value }: { value: number | null }) {
  // Null is *not* zero: "not measurable" and "measured as nothing" are
  // different findings, and the promotion gate treats a metric that stopped
  // being measurable as a regression rather than a pass. Rendering null as
  // 0.00 would erase that distinction on the one screen where it matters.
  if (value === null || value === undefined) {
    return <span className="text-ink-400" title="not measurable">—</span>;
  }
  return <span className="tnum">{value.toFixed(3)}</span>;
}

function GateVerdict({ run }: { run: EvalRunDetail }) {
  if (!run.gate) return null;
  const g = run.gate;
  return (
    <div
      className={`mt-2 rounded-xl border px-3 py-2 text-[12px] ${
        g.would_promote
          ? "border-ok/30 bg-ok/5 text-ink-700"
          : "border-warn/30 bg-warn/5 text-ink-700"
      }`}
    >
      <div className="font-semibold">
        {g.would_promote ? "Clears the gate" : "Blocked by the gate"}
      </div>
      <ul className="mt-1 list-disc space-y-0.5 pl-4">
        {g.reasons.map((r) => (
          <li key={r}>{r}</li>
        ))}
      </ul>
      {Object.keys(g.deltas).length > 0 && (
        <div className="tnum mt-1 flex flex-wrap gap-x-3 text-[11px] text-ink-500">
          {Object.entries(g.deltas).map(([metric, d]) => (
            <span key={metric} className={d < 0 ? "text-warn" : ""}>
              {metric} {d >= 0 ? "+" : ""}
              {d.toFixed(4)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export function EvalPanel({ me }: { me: Me }) {
  const mayRead = can(me, Cap.EVAL_READ);
  const mayRun = can(me, Cap.EVAL_RUN);
  const mayPromote = can(me, Cap.RELEASE_PROMOTE);

  const { data, error, loading, reload } = useResource(
    fetchEvalDatasets,
    [],
    mayRead,
  );
  const [open, setOpen] = useState<string | null>(null);
  const [reviewing, setReviewing] = useState<string | null>(null);
  const [run, setRun] = useState<EvalRunDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);

  const act = async (fn: () => Promise<unknown>, message: string) => {
    setBusy(true);
    setActionError(null);
    try {
      await fn();
      setNote(message);
      await reload();
    } catch (err) {
      setActionError(err);
    } finally {
      setBusy(false);
    }
  };

  const openRun = async (runId: string) => {
    if (open === runId) {
      setOpen(null);
      setRun(null);
      return;
    }
    setOpen(runId);
    setRun(null);
    try {
      setRun(await fetchEvalRun(runId));
    } catch (err) {
      setActionError(err);
    }
  };

  return (
    <Panel
      title="Evaluation"
      subtitle="A golden set is versioned by the hash of its questions, so a run always names the exact set it was scored on. Promotion is comparative: a candidate is measured against the baseline on that same set, and a metric that stopped being measurable counts as a regression rather than a pass."
      actions={
        mayRead ? (
          <button type="button" onClick={() => void reload()} className="btn-ghost">
            Refresh
          </button>
        ) : null
      }
    >
      {!mayRead ? (
        <Denied capability={Cap.EVAL_READ} />
      ) : loading ? (
        <Spinner />
      ) : error ? (
        <ErrorNote error={error} />
      ) : !data?.datasets.length ? (
        <Empty>
          No golden sets yet. One is authored with{" "}
          <code className="font-mono">PUT /api/eval/datasets/&lt;name&gt;</code>,
          which carries <code className="font-mono">release:promote</code> —
          whoever can rewrite the questions can make any regression pass.
        </Empty>
      ) : (
        <>
          {note ? (
            <p className="mb-2 rounded-xl border border-ok/30 bg-ok/5 px-3 py-1.5 text-[12px] text-ink-600">
              {note}
            </p>
          ) : null}
          {actionError ? (
            <div className="mb-2">
              <ErrorNote error={actionError} />
            </div>
          ) : null}

          <Table
            head={[
              "dataset",
              "collection",
              "items",
              "recall",
              "pass rate",
              "baseline",
              "",
            ]}
          >
            {data.datasets.map((d) => {
              const latest = d.latest_run;
              return (
                <tr key={d.name} className="border-b border-cream-200 align-top">
                  <td className="py-2 pr-3">
                    <div className="font-medium text-ink-800">{d.name}</div>
                    <div className="font-mono text-[11px] text-ink-400">
                      {d.content_sha256.slice(0, 12)}…
                    </div>
                  </td>
                  <td className="py-2 pr-3 font-mono text-[11px]">{d.collection}</td>
                  <td className="tnum py-2 pr-3">
                    {latest ? (
                      <span title="scoreable / run">
                        {latest.items_scoreable}/{latest.items_run}
                      </span>
                    ) : (
                      d.item_count
                    )}
                  </td>
                  <td className="py-2 pr-3">
                    <Metric value={latest?.retrieval_recall ?? null} />
                  </td>
                  <td className="py-2 pr-3">
                    <Metric value={latest?.answer_pass_rate ?? null} />
                  </td>
                  <td className="py-2 pr-3 text-[11px]">
                    {d.baseline_run_id ? (
                      <span className="text-ink-500">
                        {latest?.is_baseline ? "latest run" : "an earlier run"}
                        {d.baseline_note ? ` · ${d.baseline_note}` : ""}
                      </span>
                    ) : (
                      <span className="text-ink-400">none yet</span>
                    )}
                  </td>
                  <td className="py-2">
                    <div className="flex flex-wrap justify-end gap-1.5">
                      <button
                        type="button"
                        disabled={busy || !mayRun}
                        title={
                          mayRun
                            ? "Queue a run through the outbox. Scores the pinned version."
                            : `Requires ${Cap.EVAL_RUN}`
                        }
                        onClick={() =>
                          void act(
                            () => startEvalRun(d.name, d.content_sha256),
                            `Queued an eval of ${d.name}. A worker has to be running.`,
                          )
                        }
                        className="btn-ghost px-2 py-1 text-[12px]"
                      >
                        Run
                      </button>
                      <button
                        type="button"
                        onClick={() =>
                          setReviewing(reviewing === d.name ? null : d.name)
                        }
                        title="Read and attest to the expected answers this set is scored against."
                        className="btn-ghost px-2 py-1 text-[12px]"
                      >
                        {reviewing === d.name ? "Close" : "Review"}
                      </button>
                      {latest ? (
                        <button
                          type="button"
                          onClick={() => void openRun(latest.run_id)}
                          className="btn-ghost px-2 py-1 text-[12px]"
                        >
                          {open === latest.run_id ? "Hide" : "Inspect"}
                        </button>
                      ) : null}
                      {latest && mayPromote && !latest.is_baseline ? (
                        <button
                          type="button"
                          disabled={busy}
                          title="Move the baseline to this run, if the gate allows."
                          onClick={() =>
                            void act(
                              () => promoteEvalRun(latest.run_id, d.name),
                              "Promotion decided — see the reasons below.",
                            )
                          }
                          className="btn-primary px-2 py-1 text-[12px]"
                        >
                          Promote
                        </button>
                      ) : null}
                    </div>
                  </td>
                </tr>
              );
            })}
          </Table>

          {reviewing ? (
            <EvalReview
              key={reviewing}
              name={reviewing}
              canWrite={mayPromote}
              onChanged={() => void reload()}
            />
          ) : null}

          {open ? (
            <div className="mt-3 rounded-xl border border-cream-300 bg-cream-50/60 p-3">
              {!run ? (
                <Spinner label="Loading run…" />
              ) : (
                <>
                  <div className="tnum flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-ink-500">
                    <span>run {run.run_id.slice(0, 8)}</span>
                    <span>set {run.dataset_sha}</span>
                    <span>rev {run.code_rev}</span>
                    <span>{run.model_id}</span>
                    <span>{run.elapsed_s.toFixed(1)}s</span>
                  </div>
                  <GateVerdict run={run} />

                  <ul className="mt-2 space-y-1.5">
                    {run.outcomes.map((o) => {
                      // An item with no evidence cannot score recall at all.
                      // Saying so beats rendering a blank that reads as zero.
                      const missed =
                        o.retrieval_recall !== null && o.retrieval_recall < 1;
                      return (
                        <li
                          key={o.item_id}
                          className={`rounded-lg border px-2 py-1.5 text-[12px] ${
                            missed
                              ? "border-warn/30 bg-warn/5"
                              : "border-cream-300 bg-white"
                          }`}
                        >
                          <div className="text-ink-800">{o.question}</div>
                          <div className="tnum mt-0.5 text-[11px] text-ink-500">
                            recall{" "}
                            {o.retrieval_recall === null ? (
                              <span title="no evidence ids — recall is not scoreable for this item">
                                n/a
                              </span>
                            ) : (
                              o.retrieval_recall.toFixed(2)
                            )}{" "}
                            · wanted {o.must_cite.length} · got {o.retrieved.length}
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                </>
              )}
            </div>
          ) : null}

          {!mayPromote ? (
            <p className="mt-2 text-[11px] leading-relaxed text-ink-400">
              You may run and read evaluations but not move a baseline or edit a
              set — that is <code className="font-mono">release:promote</code>.
              Measuring is deliberately cheaper than deciding: making people ask
              permission to measure is how measurement stops happening.
            </p>
          ) : null}
        </>
      )}
    </Panel>
  );
}
