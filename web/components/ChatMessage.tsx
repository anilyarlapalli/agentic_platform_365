"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { QueryResponse, Source } from "@/lib/api";

export type Turn =
  | { role: "user"; content: string }
  | { role: "assistant"; content: string; result: QueryResponse }
  | { role: "error"; content: string };

const SIGNAL_STYLE: Record<string, string> = {
  dense: "bg-signal-dense/10 text-signal-dense",
  lexical: "bg-signal-lexical/10 text-signal-lexical",
  graph: "bg-signal-graph/10 text-signal-graph",
};

function SignalChips({ signals }: { signals: string[] | null }) {
  if (!signals || signals.length === 0) return null;
  return (
    <span className="flex flex-wrap gap-1">
      {signals.map((s) => (
        <span
          key={s}
          className={`chip ${SIGNAL_STYLE[s] || "bg-ink-300/20 text-ink-600"}`}
          title={`Retrieved by the ${s} signal`}
        >
          {s}
        </span>
      ))}
    </span>
  );
}

function SourceCard({ source, index }: { source: Source; index: number }) {
  const [open, setOpen] = useState(false);
  // Dense returns cosine distance (lower is closer); graph returns a fused
  // score (higher is better). Showing them under one "relevance" label would
  // invert the meaning for one of the two modes.
  const metric =
    source.score != null
      ? { label: "score", value: source.score.toFixed(4), hint: "higher is better" }
      : source.distance != null
        ? { label: "distance", value: source.distance.toFixed(4), hint: "lower is closer" }
        : null;

  return (
    <li className="rounded-xl border border-cream-300 bg-cream-50/60 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="chip bg-ink-900/5 font-mono text-ink-600">[{index + 1}]</span>
        <code className="truncate font-mono text-[11.5px] text-ink-700" title={source.canonical_id}>
          {source.canonical_id}
        </code>
        <SignalChips signals={source.signals} />
        {metric ? (
          <span className="tnum ml-auto text-[11px] text-ink-400" title={metric.hint}>
            {metric.label} {metric.value}
          </span>
        ) : null}
      </div>
      <p
        className={`mt-1.5 whitespace-pre-wrap text-[12.5px] leading-relaxed text-ink-600 ${
          open ? "" : "line-clamp-2"
        }`}
      >
        {source.text}
      </p>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="mt-1 text-[11px] font-medium text-copper-600 hover:underline"
      >
        {open ? "less" : "more"}
      </button>
    </li>
  );
}

function Metrics({ r }: { r: QueryResponse }) {
  const items: Array<[string, string, string?]> = [
    ["mode", r.mode],
    ["latency", `${r.latency_ms.toFixed(0)} ms`],
    ["tokens", `${r.input_tokens} in / ${r.output_tokens} out`],
    ["cost", `$${r.cost_usd.toFixed(6)}`],
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-400">
      {items.map(([k, v]) => (
        <span key={k} className="tnum">
          <span className="text-ink-300">{k}</span> {v}
        </span>
      ))}
      {r.cache_hit ? (
        <span className="chip bg-ok/10 text-ok" title="Served from the semantic cache; no model call was made">
          cache hit
        </span>
      ) : null}
      <span
        className={`chip ${r.grounded ? "bg-ok/10 text-ok" : "bg-warn/10 text-warn"}`}
        title={
          r.grounded
            ? "The answer was produced with retrieved context"
            : "No context was retrieved — the answer is not grounded in the corpus"
        }
      >
        {r.grounded ? "grounded" : "ungrounded"}
      </span>
    </div>
  );
}

function GraphPanel({ r }: { r: QueryResponse }) {
  if (!r.graph) return null;
  const g = r.graph;
  return (
    <div className="mt-3">
      {/* The defect this platform ships a warning for. An edgeless graph
          answers exactly like a populated one, with no error and worse
          retrieval — so it is stated loudly rather than left in a stats row. */}
      {g.edgeless ? (
        <div className="mb-2 rounded-xl border border-warn/30 bg-warn/5 p-3">
          <div className="text-[12.5px] font-semibold text-warn">
            Graph has {g.nodes} nodes and no edges
          </div>
          <p className="mt-1 text-[12px] leading-relaxed text-ink-600">
            Entity matching still works, so this answer improved on dense
            retrieval — but neighbour traversal did not run. Relation extraction
            needs the onboarding artifacts (instance table and predicate map);
            without them it returns empty. This is{" "}
            <strong>entity-match GraphRAG, not traversal GraphRAG</strong>. Run
            onboarding for <code className="font-mono">{g.schema_domain}</code>{" "}
            to populate edges.
          </p>
        </div>
      ) : null}

      <div className="tnum flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-ink-400">
        <span>
          <span className="text-ink-300">schema</span> {g.schema_domain}
        </span>
        <span>
          <span className="text-ink-300">nodes</span> {g.nodes}
        </span>
        <span className={g.edges === 0 ? "text-warn" : ""}>
          <span className="text-ink-300">edges</span> {g.edges}
        </span>
        <span>
          <span className="text-ink-300">docs</span> {g.documents}
        </span>
        <span>
          <span className="text-ink-300">build</span> {g.build_ms.toFixed(0)} ms
        </span>
      </div>
    </div>
  );
}

export function ChatMessage({ turn }: { turn: Turn }) {
  if (turn.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-bubble bg-copper-600 px-4 py-2.5 text-[15px] leading-relaxed text-white">
          {turn.content}
        </div>
      </div>
    );
  }

  if (turn.role === "error") {
    return (
      <div className="rounded-xl border border-danger/30 bg-danger/5 px-4 py-3 text-[13px] text-danger">
        {turn.content}
      </div>
    );
  }

  const r = turn.result;
  return (
    <div className="space-y-3">
      <div className="max-w-none rounded-bubble border border-cream-300 bg-white px-4 py-3 shadow-ring">
        <div className="prose-sm text-[15px] leading-relaxed text-ink-800 [&_code]:rounded [&_code]:bg-cream-100 [&_code]:px-1 [&_code]:font-mono [&_code]:text-[13px] [&_li]:my-0.5 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-2 [&_strong]:font-semibold [&_table]:my-2 [&_table]:block [&_table]:overflow-x-auto [&_td]:border [&_td]:border-cream-300 [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:border-cream-300 [&_th]:bg-cream-100 [&_th]:px-2 [&_th]:py-1 [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{turn.content}</ReactMarkdown>
        </div>

        <div className="mt-3 border-t border-cream-200 pt-2">
          <Metrics r={r} />
          <GraphPanel r={r} />
        </div>
      </div>

      {r.sources.length > 0 ? (
        <details className="rounded-xl border border-cream-300 bg-white/60 px-3 py-2" open>
          <summary className="cursor-pointer select-none text-[12px] font-medium text-ink-600">
            {r.sources.length} source{r.sources.length === 1 ? "" : "s"}
          </summary>
          <ul className="mt-2 space-y-2">
            {r.sources.map((s, i) => (
              <SourceCard key={`${s.canonical_id}-${i}`} source={s} index={i} />
            ))}
          </ul>
          {r.retrieval ? (
            <pre className="mt-2 overflow-x-auto rounded-lg bg-cream-100 p-2 font-mono text-[11px] text-ink-600">
              {JSON.stringify(r.retrieval, null, 2)}
            </pre>
          ) : null}
        </details>
      ) : (
        <div className="rounded-xl border border-warn/30 bg-warn/5 px-3 py-2 text-[12px] text-warn">
          No sources were retrieved for this question.
        </div>
      )}
    </div>
  );
}
