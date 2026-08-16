"use client";

import { useState } from "react";
import { Cap, can, fetchUsage, updateCaps, type Me } from "@/lib/api";
import { Denied, ErrorNote, Panel, Spinner, useResource } from "./Panel";

export function UsagePanel({ me }: { me: Me }) {
  const mayRead = can(me, Cap.USAGE_READ);
  const mayManage = can(me, Cap.BUDGET_MANAGE);

  const { data, error, loading, reload } = useResource(fetchUsage, [], mayRead);
  const [daily, setDaily] = useState("");
  const [monthly, setMonthly] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setSaveError(null);
    try {
      await updateCaps({
        daily_token_cap: daily === "" ? null : Number(daily),
        monthly_cost_cap_usd: monthly === "" ? null : Number(monthly),
      });
      setDaily("");
      setMonthly("");
      await reload();
    } catch (err) {
      setSaveError(err);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Panel
      title="Budget"
      subtitle="Reading spend and changing a ceiling are separate authorities — an operator can see the cap but not raise it."
    >
      {!mayRead ? (
        <Denied capability={Cap.USAGE_READ} />
      ) : loading ? (
        <Spinner />
      ) : error ? (
        <ErrorNote error={error} />
      ) : data ? (
        <>
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="daily token cap" value={data.daily_token_cap.toLocaleString()} />
            <Stat label="monthly cost cap" value={`$${data.monthly_cost_cap_usd}`} />
            {/* Null is rendered as "not recorded", never as 0. The ledger lands
                in a later phase, and showing 0 would read as "spent nothing"
                when the truth is "nothing is being measured yet". */}
            <Stat
              label="tokens today"
              value={data.tokens_today?.toLocaleString() ?? "not recorded"}
              muted={data.tokens_today == null}
            />
            <Stat
              label="cost this month"
              value={
                data.cost_this_month_usd != null
                  ? `$${data.cost_this_month_usd}`
                  : "not recorded"
              }
              muted={data.cost_this_month_usd == null}
            />
          </dl>

          <p className="mt-3 text-[12px] leading-relaxed text-ink-500">
            When the ledger is unreadable this tenant fails{" "}
            <strong>{data.fail_closed ? "closed" : "open"}</strong> on background
            work. Interactive paths fail open regardless: one chat turn costs
            cents, a corpus rebuild does not.
          </p>

          {mayManage ? (
            <form onSubmit={save} className="mt-4 border-t border-cream-200 pt-3">
              <div className="flex flex-wrap items-end gap-2">
                <label className="flex-1">
                  <span className="label">New daily token cap</span>
                  <input
                    type="number"
                    min={0}
                    value={daily}
                    onChange={(e) => setDaily(e.target.value)}
                    placeholder="unchanged"
                    className="input tnum"
                  />
                </label>
                <label className="flex-1">
                  <span className="label">New monthly cost cap (USD)</span>
                  <input
                    type="number"
                    min={0}
                    step="0.01"
                    value={monthly}
                    onChange={(e) => setMonthly(e.target.value)}
                    placeholder="unchanged"
                    className="input tnum"
                  />
                </label>
                <button
                  type="submit"
                  disabled={saving || (daily === "" && monthly === "")}
                  className="btn-primary"
                >
                  {saving ? "Saving…" : "Update caps"}
                </button>
              </div>
              <p className="mt-1.5 text-[11px] text-ink-400">
                Blank fields are left unchanged. Written through the owner role —
                the application role cannot raise its own ceiling.
              </p>
              {saveError ? (
                <div className="mt-2">
                  <ErrorNote error={saveError} />
                </div>
              ) : null}
            </form>
          ) : (
            <div className="mt-4 border-t border-cream-200 pt-3">
              <Denied capability={Cap.BUDGET_MANAGE} />
            </div>
          )}
        </>
      ) : null}
    </Panel>
  );
}

function Stat({
  label,
  value,
  muted,
}: {
  label: string;
  value: string;
  muted?: boolean;
}) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-ink-400">{label}</dt>
      <dd
        className={`tnum mt-0.5 text-[15px] ${
          muted ? "italic text-ink-400" : "text-ink-900"
        }`}
      >
        {value}
      </dd>
    </div>
  );
}
