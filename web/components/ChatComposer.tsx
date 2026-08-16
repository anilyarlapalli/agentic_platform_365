"use client";

import { useEffect, useRef } from "react";
import type { OnboardedDomain, QueryMode } from "@/lib/api";

type Props = {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  mode: QueryMode;
  onModeChange: (m: QueryMode) => void;
  collection: string;
  onCollectionChange: (c: string) => void;
  schemaDomain: string;
  onSchemaDomainChange: (d: string) => void;
  /** Published taxonomies only — an unpublished one resolves to no artifacts. */
  domains: OnboardedDomain[];
  disabled?: boolean;
};

export function ChatComposer({
  value,
  onChange,
  onSubmit,
  mode,
  onModeChange,
  collection,
  onCollectionChange,
  schemaDomain,
  onSchemaDomainChange,
  domains,
  disabled = false,
}: Props) {
  const taRef = useRef<HTMLTextAreaElement>(null);

  // Auto-grow to about six lines, then scroll.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, 220)}px`;
  }, [value]);

  const handleKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!disabled && value.trim()) onSubmit();
    }
  };

  return (
    <div className="pointer-events-none sticky bottom-0 z-10 w-full bg-gradient-to-t from-cream-50 via-cream-50/95 to-transparent pb-6 pt-6">
      <div className="pointer-events-auto mx-auto w-full max-w-3xl px-3">
        <div className="rounded-3xl border border-cream-300 bg-white px-4 py-3 shadow-soft focus-within:border-copper-400 focus-within:ring-2 focus-within:ring-copper-400/20">
          <div className="flex items-end gap-2">
            <textarea
              ref={taRef}
              value={value}
              disabled={disabled}
              placeholder="Ask a question about this collection…"
              rows={1}
              onChange={(e) => onChange(e.target.value)}
              onKeyDown={handleKey}
              className="block w-full resize-none border-0 bg-transparent text-[15px] leading-relaxed text-ink-800 placeholder:text-ink-400 focus:outline-none focus:ring-0 disabled:opacity-50"
            />
            <button
              type="button"
              onClick={onSubmit}
              disabled={disabled || !value.trim()}
              className="btn-primary shrink-0 rounded-full"
              title="Send (Enter)"
            >
              {disabled ? "Thinking…" : "Send"}
            </button>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-3 border-t border-cream-200 pt-3">
            {/* Mode is a first-class control, not a setting buried in a menu:
                the two modes retrieve differently and cost differently, and the
                answer is only interpretable if you know which one produced it. */}
            <div className="inline-flex rounded-lg border border-cream-300 p-0.5">
              {(["dense", "graph"] as QueryMode[]).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => onModeChange(m)}
                  disabled={disabled}
                  className={`rounded-md px-2.5 py-1 text-xs font-medium transition disabled:opacity-50 ${
                    mode === m
                      ? "bg-copper-600 text-white"
                      : "text-ink-600 hover:bg-cream-100"
                  }`}
                  title={
                    m === "dense"
                      ? "Vector similarity only. No schema dependency."
                      : "Adds lexical retrieval and knowledge-graph entity matching, fused."
                  }
                >
                  {m}
                </button>
              ))}
            </div>

            <label className="flex items-center gap-1.5 text-xs text-ink-500">
              collection
              <input
                value={collection}
                onChange={(e) => onCollectionChange(e.target.value)}
                disabled={disabled}
                className="w-36 rounded-md border border-cream-300 px-2 py-1 font-mono text-xs text-ink-800 focus:border-copper-400 focus:outline-none disabled:opacity-50"
              />
            </label>

            {/* Only in graph mode, because dense retrieval has no schema
                dependency at all — offering the control there would imply the
                choice changes something.

                A picker rather than a text field: the domain name is looked up
                against *published* onboarding artifacts, and a name with nothing
                published resolves to an edgeless graph with no error. Typing is
                the one input method that cannot be validated before the query
                is spent. */}
            {mode === "graph" ? (
              <label className="flex items-center gap-1.5 text-xs text-ink-500">
                schema
                {domains.length ? (
                  <select
                    value={schemaDomain}
                    onChange={(e) => onSchemaDomainChange(e.target.value)}
                    disabled={disabled}
                    className="rounded-md border border-cream-300 px-2 py-1 font-mono text-xs text-ink-800 focus:border-copper-400 focus:outline-none disabled:opacity-50"
                  >
                    {/* A domain the caller is already using but which is no
                        longer published stays selectable, so switching modes
                        cannot silently rewrite the query behind them. */}
                    {!domains.some((d) => d.domain === schemaDomain) ? (
                      <option value={schemaDomain}>
                        {schemaDomain} — not published
                      </option>
                    ) : null}
                    {domains.map((d) => (
                      <option key={d.domain} value={d.domain}>
                        {d.domain} · {d.published?.collection}
                        {d.published?.relations_available ? "" : " — no edges"}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span
                    className="rounded-md border border-warn/30 bg-warn/5 px-2 py-1 text-[11px] text-warn"
                    title="build_graph resolves artifacts by domain name; with none published every graph is edgeless."
                  >
                    none published — graphs will have no edges
                  </span>
                )}
              </label>
            ) : null}

            <span className="ml-auto text-[11px] text-ink-400">
              <kbd className="rounded bg-cream-200 px-1.5 py-0.5">Enter</kbd>{" "}
              send ·{" "}
              <kbd className="rounded bg-cream-200 px-1.5 py-0.5">Shift</kbd>+
              <kbd className="rounded bg-cream-200 px-1.5 py-0.5">Enter</kbd>{" "}
              newline
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
