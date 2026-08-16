"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api";

export function Panel({
  title,
  subtitle,
  actions,
  children,
}: {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="card p-4">
      <div className="mb-3 flex flex-wrap items-start gap-2">
        <div>
          <h2 className="font-serif text-lg text-ink-900">{title}</h2>
          {subtitle ? (
            <p className="mt-0.5 text-[12.5px] leading-relaxed text-ink-500">
              {subtitle}
            </p>
          ) : null}
        </div>
        {actions ? <div className="ml-auto flex gap-2">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

/** Shown in place of a panel's body when the caller lacks the capability. */
export function Denied({ capability }: { capability: string }) {
  return (
    <div className="rounded-xl border border-cream-300 bg-cream-100/60 px-3 py-2.5 text-[12.5px] text-ink-500">
      Requires <code className="font-mono text-ink-700">{capability}</code>. Your
      roles do not grant it, so this panel is read-locked rather than hidden —
      knowing the control exists is part of understanding the policy.
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  const isApi = error instanceof ApiError;
  const forbidden = isApi && error.isForbidden;
  return (
    <div
      className={`rounded-xl border px-3 py-2 text-[12.5px] ${
        forbidden
          ? "border-warn/30 bg-warn/5 text-warn"
          : "border-danger/30 bg-danger/5 text-danger"
      }`}
    >
      {error instanceof Error ? error.message : String(error)}
    </div>
  );
}

/**
 * Load-on-mount with an explicit reload, sharing one error and loading shape
 * across every panel.
 *
 * `enabled` exists so a panel the caller cannot use never fires the request at
 * all — issuing a call that is certain to 403 would fill the audit log with
 * denials the user did not attempt.
 */
export function useResource<T>(
  loader: () => Promise<T>,
  deps: React.DependencyList,
  enabled = true,
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(enabled);

  const reload = useCallback(async () => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setData(await loader());
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { data, error, loading, reload, setError };
}

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return <div className="py-3 text-[12.5px] text-ink-400">{label}</div>;
}

export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-cream-300 px-3 py-4 text-center text-[12.5px] text-ink-400">
      {children}
    </div>
  );
}

export function Table({
  head,
  children,
}: {
  head: string[];
  children: React.ReactNode;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[520px] border-collapse text-left text-[12.5px]">
        <thead>
          <tr className="border-b border-cream-300">
            {head.map((h) => (
              <th
                key={h}
                className="px-2 py-1.5 text-[11px] font-medium uppercase tracking-wide text-ink-400"
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
