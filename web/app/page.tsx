"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  Cap,
  DEFAULT_SCHEMA_DOMAIN,
  can,
  fetchOnboardedDomains,
  query,
  type Me,
  type OnboardedDomain,
  type QueryMode,
} from "@/lib/api";
import { ChatComposer } from "@/components/ChatComposer";
import { ChatMessage, type Turn } from "@/components/ChatMessage";
import { Shell } from "@/components/Shell";

const SUGGESTIONS = [
  "What is the recommended torque for spindle SA-400?",
  "Which maintenance procedures mention bearing replacement?",
  "Summarise the safety steps before servicing a spindle.",
];

function Chat({ me }: { me: Me }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [mode, setMode] = useState<QueryMode>("dense");
  const [collection, setCollection] = useState("maintenance");
  const [busy, setBusy] = useState(false);
  // Which onboarded taxonomy graph mode should resolve artifacts against.
  // This used to be absent, so every graph query asked for "manufacturing"
  // whether or not anything was published under that name — and an unpublished
  // domain yields a graph with entities, no edges, and no error.
  const [schemaDomain, setSchemaDomain] = useState(DEFAULT_SCHEMA_DOMAIN);
  const [domains, setDomains] = useState<OnboardedDomain[]>([]);
  // Carried across turns so the API can thread the conversation. It is issued
  // by the server on the first answer and bound to this principal there —
  // supplying someone else's id returns 404, not another tenant's session.
  const [sessionId, setSessionId] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, busy]);

  // Published taxonomies, fetched once. Failure is swallowed on purpose: an
  // operator without onboarding:read can still chat, and the picker simply
  // falls back to the default rather than the page failing to render.
  useEffect(() => {
    let live = true;
    fetchOnboardedDomains()
      .then(({ domains: all }) => {
        if (!live) return;
        const published = all.filter((d) => d.published);
        setDomains(published);
        // Prefer a domain that can actually traverse. Selecting one that is
        // published but has no predicate map would look like a working choice
        // and produce the edgeless result this control exists to prevent.
        const best =
          published.find((d) => d.published?.relations_available) ?? published[0];
        if (best) setSchemaDomain(best.domain);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  const send = useCallback(
    async (text: string) => {
      const question = text.trim();
      if (!question || busy) return;

      setTurns((t) => [...t, { role: "user", content: question }]);
      setDraft("");
      setBusy(true);

      try {
        const result = await query({
          question,
          collection,
          mode,
          schema_domain: schemaDomain,
          session_id: sessionId,
        });
        setSessionId(result.session_id);
        setTurns((t) => [
          ...t,
          { role: "assistant", content: result.answer, result },
        ]);
      } catch (err) {
        // Each of these is a distinct, deliberate server decision. Collapsing
        // them into "something went wrong" would hide the one case the user can
        // actually act on.
        let message: string;
        if (err instanceof ApiError && err.isBudgetExceeded) {
          message =
            `Refused by a budget ceiling: ${err.message} ` +
            `This is deliberate, not a failure — retry once the window resets, ` +
            `or raise the cap in Admin if you hold budget:manage.`;
        } else if (err instanceof ApiError && err.status === 503) {
          message = `The model provider is unavailable: ${err.message} Retrying may succeed.`;
        } else if (err instanceof ApiError && err.status === 404) {
          message =
            "That session is no longer available. Starting a fresh one — ask again.";
          setSessionId(null);
        } else if (err instanceof ApiError && err.isForbidden) {
          message = `Not permitted: ${err.message}`;
        } else {
          message = err instanceof Error ? err.message : String(err);
        }
        setTurns((t) => [...t, { role: "error", content: message }]);
      } finally {
        setBusy(false);
      }
    },
    [busy, collection, mode, schemaDomain, sessionId],
  );

  if (!can(me, Cap.QUERY_READ)) {
    return (
      <main className="mx-auto max-w-3xl px-4 py-16 text-center">
        <h1 className="font-serif text-2xl text-ink-900">No query access</h1>
        <p className="mt-2 text-sm text-ink-500">
          Your roles ({me.roles.join(", ") || "none"}) do not include{" "}
          <code className="font-mono">query:read</code>.
        </p>
      </main>
    );
  }

  return (
    <main className="mx-auto flex min-h-[calc(100vh-49px)] max-w-3xl flex-col px-4">
      <div className="flex-1 space-y-5 py-6">
        {turns.length === 0 ? (
          <div className="pt-10">
            <h1 className="font-serif text-2xl text-ink-900">
              Ask {me.tenant} a question
            </h1>
            <p className="mt-1.5 text-[13.5px] leading-relaxed text-ink-500">
              Answers are grounded in this tenant&rsquo;s indexed sources only.
              Switch to <code className="font-mono">graph</code> mode to add
              lexical retrieval and knowledge-graph entity matching, and every
              source will show which signal retrieved it.
            </p>
            <div className="mt-5 space-y-2">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => void send(s)}
                  className="block w-full rounded-xl border border-cream-300 bg-white px-3.5 py-2.5 text-left text-[13.5px] text-ink-700 transition hover:border-copper-400/50 hover:bg-cream-50"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          turns.map((t, i) => <ChatMessage key={i} turn={t} />)
        )}

        {busy ? (
          <div className="flex items-center gap-2 text-[13px] text-ink-400">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-copper-500" />
            {mode === "graph"
              ? "Building the graph and retrieving…"
              : "Retrieving…"}
          </div>
        ) : null}
        <div ref={endRef} />
      </div>

      <ChatComposer
        value={draft}
        onChange={setDraft}
        onSubmit={() => void send(draft)}
        mode={mode}
        onModeChange={setMode}
        collection={collection}
        onCollectionChange={setCollection}
        schemaDomain={schemaDomain}
        onSchemaDomainChange={setSchemaDomain}
        domains={domains}
        disabled={busy}
      />
    </main>
  );
}

export default function Page() {
  return <Shell>{(me) => <Chat me={me} />}</Shell>;
}
