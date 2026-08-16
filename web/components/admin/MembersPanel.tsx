"use client";

import { useState } from "react";
import { Cap, can, createGrant, fetchMembers, type Me } from "@/lib/api";
import { Denied, Empty, ErrorNote, Panel, Spinner, useResource } from "./Panel";

export function MembersPanel({ me }: { me: Me }) {
  const mayManage = can(me, Cap.MEMBER_MANAGE);
  const { data, error, loading, reload } = useResource(fetchMembers, [], mayManage);

  const [principalId, setPrincipalId] = useState("");
  const [capability, setCapability] = useState("");
  const [resource, setResource] = useState("*");
  const [expiresAt, setExpiresAt] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<unknown>(null);

  // Only capabilities the signed-in principal actually holds are offered. The
  // server refuses to grant one the granter lacks — otherwise member:manage
  // would be equivalent to owner, since a manager could grant themselves
  // everything missing. Offering the full enum here would just produce 403s.
  const grantable = me.capabilities;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setActionError(null);
    try {
      await createGrant({
        principal_id: principalId,
        capability,
        resource: resource || "*",
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
      });
      setPrincipalId("");
      setCapability("");
      setResource("*");
      setExpiresAt("");
      await reload();
    } catch (err) {
      setActionError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel
      title="Members and grants"
      subtitle="Roles map to capabilities; a grant adds one directly, scoped to a resource and optionally expiring. That is what a per-collection reviewer is, without a new concept."
    >
      {!mayManage ? (
        <Denied capability={Cap.MEMBER_MANAGE} />
      ) : loading ? (
        <Spinner />
      ) : error ? (
        <ErrorNote error={error} />
      ) : (
        <>
          {!data || data.members.length === 0 ? (
            <Empty>No principals in this tenant.</Empty>
          ) : (
            <ul className="space-y-2">
              {data.members.map((m) => (
                <li key={m.id} className="rounded-xl border border-cream-300 p-2.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[13px] font-medium text-ink-900">
                      {m.subject}
                    </span>
                    {m.roles.map((r) => (
                      <span key={r} className="chip bg-copper-600/10 text-copper-700">
                        {r}
                      </span>
                    ))}
                    <span className="chip bg-ink-400/10 text-ink-500">
                      {m.actor_type}
                    </span>
                    {m.disabled ? (
                      <span className="chip bg-danger/10 text-danger">disabled</span>
                    ) : null}
                    <button
                      type="button"
                      onClick={() => setPrincipalId(m.id)}
                      className="ml-auto text-[11px] font-medium text-copper-600 hover:underline"
                    >
                      grant to this member
                    </button>
                  </div>
                  {m.grants.length > 0 ? (
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {m.grants.map((g) => (
                        <span
                          key={g.id}
                          className="chip bg-signal-graph/10 font-mono text-signal-graph"
                          title={
                            g.expires_at
                              ? `on ${g.resource}, expires ${new Date(g.expires_at).toLocaleString()}`
                              : `on ${g.resource}, no expiry`
                          }
                        >
                          {g.capability}
                          {g.resource !== "*" ? ` @ ${g.resource}` : ""}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <div className="mt-1 text-[11.5px] text-ink-400">
                      No direct grants — authority comes from roles alone.
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}

          <form onSubmit={submit} className="mt-4 border-t border-cream-200 pt-3">
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <label>
                <span className="label">Principal id</span>
                <input
                  value={principalId}
                  onChange={(e) => setPrincipalId(e.target.value)}
                  required
                  placeholder="click “grant to this member” above"
                  className="input font-mono text-[12px]"
                />
              </label>
              <label>
                <span className="label">Capability</span>
                <select
                  value={capability}
                  onChange={(e) => setCapability(e.target.value)}
                  required
                  className="input font-mono text-[12px]"
                >
                  <option value="">select…</option>
                  {grantable.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span className="label">Resource</span>
                <input
                  value={resource}
                  onChange={(e) => setResource(e.target.value)}
                  placeholder="* for any"
                  className="input font-mono text-[12px]"
                />
              </label>
              <label>
                <span className="label">Expires (optional)</span>
                <input
                  type="datetime-local"
                  value={expiresAt}
                  onChange={(e) => setExpiresAt(e.target.value)}
                  className="input text-[12px]"
                />
              </label>
            </div>
            <div className="mt-2 flex items-center gap-2">
              <button
                type="submit"
                disabled={busy || !principalId || !capability}
                className="btn-primary"
              >
                {busy ? "Granting…" : "Grant capability"}
              </button>
              <span className="text-[11px] text-ink-400">
                Only capabilities you hold are listed — you cannot grant what you
                lack.
              </span>
            </div>
            {actionError ? (
              <div className="mt-2">
                <ErrorNote error={actionError} />
              </div>
            ) : null}
          </form>
        </>
      )}
    </Panel>
  );
}
