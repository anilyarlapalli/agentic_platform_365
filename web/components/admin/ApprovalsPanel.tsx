"use client";

import { useState } from "react";
import { Cap, can, decideApproval, fetchApprovals, type Me } from "@/lib/api";
import { Denied, Empty, ErrorNote, Panel, Spinner, useResource } from "./Panel";

export function ApprovalsPanel({ me }: { me: Me }) {
  const mayApprove = can(me, Cap.TOOL_APPROVE);
  const [status, setStatus] = useState("pending");
  const { data, error, loading, reload } = useResource(
    () => fetchApprovals(status),
    [status],
    mayApprove,
  );
  const [busyId, setBusyId] = useState<string | null>(null);
  const [note, setNote] = useState<Record<string, string>>({});
  const [actionError, setActionError] = useState<unknown>(null);

  const decide = async (id: string, approved: boolean) => {
    setBusyId(id);
    setActionError(null);
    try {
      await decideApproval(id, approved, note[id]);
      await reload();
    } catch (err) {
      setActionError(err);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Panel
      title="Tool approvals"
      subtitle="Maker cannot be checker — enforced by a database constraint as well as a capability check, so no code path can bypass it."
      actions={
        mayApprove ? (
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="input w-32 py-1 text-[12px]"
          >
            {["pending", "approved", "rejected"].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        ) : null
      }
    >
      {!mayApprove ? (
        <Denied capability={Cap.TOOL_APPROVE} />
      ) : loading ? (
        <Spinner />
      ) : error ? (
        <ErrorNote error={error} />
      ) : !data || data.approvals.length === 0 ? (
        <Empty>No {status} approvals.</Empty>
      ) : (
        <div className="space-y-2">
          {actionError ? <ErrorNote error={actionError} /> : null}
          {data.approvals.map((a) => {
            const expired = new Date(a.expires_at).getTime() < Date.now();
            // Self-approval is refused server-side regardless; disabling the
            // control here explains *why* before the click rather than after.
            const blocked = a.is_own_request || expired;
            return (
              <div key={a.id} className="rounded-xl border border-cream-300 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <code className="font-mono text-[13px] font-medium text-ink-900">
                    {a.tool_name}
                  </code>
                  <span className="chip bg-warn/10 text-warn">{a.side_effect}</span>
                  {a.is_own_request ? (
                    <span className="chip bg-danger/10 text-danger">your request</span>
                  ) : null}
                  {expired ? (
                    <span className="chip bg-ink-400/15 text-ink-500">expired</span>
                  ) : null}
                  <span className="tnum ml-auto text-[11px] text-ink-400">
                    run {a.run_id.slice(0, 8)} · expires{" "}
                    {new Date(a.expires_at).toLocaleString()}
                  </span>
                </div>

                {/* The exact arguments are shown because the approval is bound
                    to their hash: approving "call the tool" and approving "call
                    the tool with these arguments" are different acts, and only
                    the second is safe. */}
                <pre className="mt-2 overflow-x-auto rounded-lg bg-cream-100 p-2 font-mono text-[11px] text-ink-700">
                  {JSON.stringify(a.arguments, null, 2)}
                </pre>

                {a.status === "pending" ? (
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <input
                      value={note[a.id] || ""}
                      onChange={(e) => setNote({ ...note, [a.id]: e.target.value })}
                      placeholder="Decision note (optional)"
                      className="input flex-1 py-1 text-[12px]"
                    />
                    <button
                      type="button"
                      onClick={() => void decide(a.id, true)}
                      disabled={busyId === a.id || blocked}
                      title={
                        a.is_own_request
                          ? "You raised this request; maker cannot be checker."
                          : expired
                            ? "This request has expired and can no longer be decided."
                            : undefined
                      }
                      className="btn-primary px-3 py-1 text-[12px]"
                    >
                      Approve
                    </button>
                    <button
                      type="button"
                      onClick={() => void decide(a.id, false)}
                      disabled={busyId === a.id || blocked}
                      className="btn-ghost px-3 py-1 text-[12px] text-danger"
                    >
                      Reject
                    </button>
                  </div>
                ) : (
                  <div className="mt-2 text-[12px] text-ink-500">
                    Decided: <strong>{a.status}</strong>. Decisions are final.
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}
