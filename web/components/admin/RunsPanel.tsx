"use client";

import { useState } from "react";
import { Cap, can, cancelRun, fetchRuns, type Me } from "@/lib/api";
import { Denied, Empty, ErrorNote, Panel, Spinner, Table, useResource } from "./Panel";

const STATUS_STYLE: Record<string, string> = {
  succeeded: "bg-ok/10 text-ok",
  failed: "bg-danger/10 text-danger",
  cancelled: "bg-ink-400/15 text-ink-500",
  pending: "bg-warn/10 text-warn",
  leased: "bg-signal-dense/10 text-signal-dense",
};

export function RunsPanel({ me }: { me: Me }) {
  const mayRead = can(me, Cap.RUN_READ);
  const mayCancel = can(me, Cap.RUN_CANCEL);
  const { data, error, loading, reload } = useResource(() => fetchRuns(50), [], mayRead);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);

  const doCancel = async (id: string) => {
    setBusyId(id);
    setActionError(null);
    try {
      await cancelRun(id);
      await reload();
    } catch (err) {
      setActionError(err);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Panel
      title="Runs"
      subtitle="The unit of work and the idempotency anchor. A run id is stable across retries, so a redelivered message resumes rather than duplicates."
      actions={
        mayRead ? (
          <button type="button" onClick={() => void reload()} className="btn-ghost">
            Refresh
          </button>
        ) : null
      }
    >
      {!mayRead ? (
        <Denied capability={Cap.RUN_READ} />
      ) : loading ? (
        <Spinner />
      ) : error ? (
        <ErrorNote error={error} />
      ) : !data || data.runs.length === 0 ? (
        <Empty>
          No runs recorded for this tenant. Queue work with{" "}
          <code className="font-mono">make e2e-transport</code>.
        </Empty>
      ) : (
        <>
          {actionError ? (
            <div className="mb-2">
              <ErrorNote error={actionError} />
            </div>
          ) : null}
          <Table head={["run", "workload", "status", "attempt", "created", ""]}>
            {data.runs.map((r) => {
              // Only pending and leased runs are cancellable; the API refuses
              // anything else with a conditional UPDATE so a run that finished
              // mid-click cannot be resurrected as cancelled.
              const cancellable = r.status === "pending" || r.status === "leased";
              return (
                <tr key={r.id} className="border-b border-cream-200 last:border-0">
                  <td className="px-2 py-1.5">
                    <code className="font-mono text-[11.5px] text-ink-600" title={r.id}>
                      {r.id.slice(0, 8)}
                    </code>
                  </td>
                  <td className="px-2 py-1.5 text-ink-700">{r.workload}</td>
                  <td className="px-2 py-1.5">
                    <span className={`chip ${STATUS_STYLE[r.status] || "bg-ink-300/20 text-ink-600"}`}>
                      {r.status}
                    </span>
                    {r.error ? (
                      <div className="mt-0.5 max-w-xs truncate text-[11px] text-danger" title={r.error}>
                        {r.error}
                      </div>
                    ) : null}
                  </td>
                  <td className="tnum px-2 py-1.5 text-ink-600">{r.attempt}</td>
                  <td className="tnum px-2 py-1.5 text-[11.5px] text-ink-500">
                    {new Date(r.created_at).toLocaleString()}
                  </td>
                  <td className="px-2 py-1.5 text-right">
                    {cancellable && mayCancel ? (
                      <button
                        type="button"
                        onClick={() => void doCancel(r.id)}
                        disabled={busyId === r.id}
                        className="btn-ghost px-2 py-1 text-[11.5px]"
                      >
                        {busyId === r.id ? "…" : "Cancel"}
                      </button>
                    ) : null}
                  </td>
                </tr>
              );
            })}
          </Table>
        </>
      )}
    </Panel>
  );
}
