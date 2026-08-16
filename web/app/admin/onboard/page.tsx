"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  Cap,
  approveOnboarding,
  can,
  cancelOnboarding,
  fetchOnboardedDomains,
  fetchOnboardingSession,
  fetchOnboardingSessions,
  editSchema,
  publishOnboarding,
  startOnboarding,
  type Me,
  type OnboardingSession,
} from "@/lib/api";
import { Shell } from "@/components/Shell";
import { Denied, Empty, ErrorNote, Panel, Spinner, useResource } from "@/components/admin/Panel";
import { CandidateQueries } from "@/components/admin/CandidateQueries";

const STATUS_STYLE: Record<string, string> = {
  drafting: "bg-signal-dense/10 text-signal-dense",
  draft_ready: "bg-warn/10 text-warn",
  approved: "bg-signal-graph/10 text-signal-graph",
  published: "bg-ok/10 text-ok",
  failed: "bg-danger/10 text-danger",
  cancelled: "bg-ink-400/15 text-ink-500",
};

/** The eight orchestrator steps, so a running draft shows a route not a spinner. */
const STEPS = [
  "sample", "extraction", "aggregation", "synthesis",
  "bootstrap_artifacts", "prompts", "safety", "clarifier", "examples", "ui",
];

function RelationsBadge({ available }: { available: boolean | undefined }) {
  if (available === undefined) return null;
  return available ? (
    <span className="chip bg-ok/10 text-ok" title="Instance table, predicate map and extraction cache are all present — the graph will traverse.">
      relations available
    </span>
  ) : (
    <span className="chip bg-warn/10 text-warn" title="Publishing this builds entities and zero edges.">
      schema-only · no edges
    </span>
  );
}

function Detail({
  session,
  me,
  onChanged,
}: {
  session: OnboardingSession;
  me: Me;
  onChanged: () => void;
}) {
  const [full, setFull] = useState<OnboardingSession | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      setFull(await fetchOnboardingSession(session.id));
    } catch (err) {
      setError(err);
    }
  }, [session.id]);

  useEffect(() => {
    void load();
    // A drafting session is doing paid work right now; poll so the step list
    // advances without the operator reloading and wondering if it hung.
    if (session.status !== "drafting") return;
    const t = setInterval(() => {
      void load();
      onChanged();
    }, 5000);
    return () => clearInterval(t);
  }, [load, session.status, onChanged]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await load();
      onChanged();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const s = full || session;
  const stats = s.stats || {};
  const isOwnDraft = s.created_by === me.principal_id;
  const done = new Set((s.progress || []).map((p) => p.step));

  return (
    <div className="rounded-xl border border-cream-300 bg-white p-3">
      <div className="flex flex-wrap items-center gap-2">
        <code className="font-mono text-[13px] font-medium text-ink-900">{s.domain}</code>
        <span className={`chip ${STATUS_STYLE[s.status]}`}>{s.status}</span>
        <RelationsBadge available={stats.relations_available} />
        <span className="ml-auto font-mono text-[11px] text-ink-400">
          {s.collection} · {new Date(s.created_at).toLocaleString()}
        </span>
      </div>

      {s.error ? (
        <div className="mt-2 rounded-lg border border-danger/30 bg-danger/5 px-2.5 py-2 text-[12px] text-danger">
          {s.error}
        </div>
      ) : null}

      {/* Step trail. Persisted server-side per step, so a draft that dies still
          shows how far it got rather than vanishing. */}
      <div className="mt-2.5 flex flex-wrap gap-1">
        {STEPS.map((step) => (
          <span
            key={step}
            className={`chip ${
              done.has(step)
                ? "bg-ok/10 text-ok"
                : s.status === "drafting"
                  ? "bg-cream-200 text-ink-400"
                  : "bg-cream-100 text-ink-300"
            }`}
          >
            {step}
          </span>
        ))}
      </div>

      {Object.keys(stats).length > 0 ? (
        <dl className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat label="instances" value={stats.instances} />
          <Stat label="predicates" value={stats.predicates} warn={stats.predicates === 0} />
          <Stat label="cache files" value={stats.cache_files} />
          <Stat label="chunks sampled" value={stats.chunks_sampled} />
        </dl>
      ) : null}

      {stats.relations_available === false && s.status !== "drafting" ? (
        <div className="mt-2 rounded-lg border border-warn/30 bg-warn/5 px-2.5 py-2 text-[12px] leading-relaxed text-ink-600">
          <strong className="text-warn">This is a schema-only bundle.</strong>{" "}
          Entity extraction will work and neighbour traversal will not. Edge-type
          synthesis needs the same relation to recur across chunks before it
          promotes it to a type — a corpus where every relation appears once
          yields a predicate map with zero entries. Publishing is still valid;
          it just will not produce edges.
        </div>
      ) : null}

      {/* The questions come before the taxonomy in the reviewer's attention:
          they are what the domain is *for*, and approving them is what makes an
          eval set possible without hand-writing chunk ids. */}
      {full && (s.stats?.candidate_queries ?? 0) >= 0 && full.schema_yaml ? (
        <CandidateQueries session={s} me={me} onSeeded={() => onChanged()} />
      ) : null}

      {full?.schema_yaml ? (
        <SchemaEditor
          session={s}
          full={full}
          me={me}
          busy={busy}
          onSaved={() => void act(async () => undefined)}
        />
      ) : null}

      {error ? (
        <div className="mt-2">
          <ErrorNote error={error} />
        </div>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-cream-200 pt-2.5">
        {s.status === "draft_ready" && can(me, Cap.SCHEMA_APPROVE) ? (
          <button
            type="button"
            onClick={() => void act(() => approveOnboarding(s.id))}
            disabled={busy || isOwnDraft}
            title={
              isOwnDraft
                ? "You drafted this schema; maker cannot be checker."
                : "Approve this taxonomy"
            }
            className="btn-primary px-3 py-1 text-[12px]"
          >
            Approve
          </button>
        ) : null}

        {s.status === "approved" && can(me, Cap.SCHEMA_APPROVE) ? (
          <button
            type="button"
            onClick={() => void act(() => publishOnboarding(s.id))}
            disabled={busy}
            title="Make this the live taxonomy and drop cached graphs"
            className="btn-primary px-3 py-1 text-[12px]"
          >
            Publish
          </button>
        ) : null}

        {(s.status === "drafting" || s.status === "draft_ready") &&
        can(me, Cap.SCHEMA_AUTHOR) ? (
          <button
            type="button"
            onClick={() => void act(() => cancelOnboarding(s.id))}
            disabled={busy}
            className="btn-ghost px-3 py-1 text-[12px] text-danger"
          >
            Cancel
          </button>
        ) : null}

        {isOwnDraft && s.status === "draft_ready" ? (
          <span className="text-[11.5px] text-ink-400">
            You drafted this — someone holding{" "}
            <code className="font-mono">schema:approve</code> must review it.
          </span>
        ) : null}
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  warn,
}: {
  label: string;
  value: number | undefined;
  warn?: boolean;
}) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-ink-400">{label}</dt>
      <dd className={`tnum mt-0.5 text-[15px] ${warn ? "text-warn" : "text-ink-900"}`}>
        {value ?? "—"}
      </dd>
    </div>
  );
}

function Onboard({ me }: { me: Me }) {
  const mayRead = can(me, Cap.SCHEMA_READ);
  const mayAuthor = can(me, Cap.SCHEMA_AUTHOR);

  const sessions = useResource(() => fetchOnboardingSessions(), [], mayRead);
  const domains = useResource(fetchOnboardedDomains, [], mayRead);

  const [domain, setDomain] = useState("");
  const [collection, setCollection] = useState("maintenance");
  const [sample, setSample] = useState(120);
  const [busy, setBusy] = useState(false);
  const [startError, setStartError] = useState<unknown>(null);

  const reloadAll = useCallback(() => {
    void sessions.reload();
    void domains.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions.reload, domains.reload]);

  const start = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setStartError(null);
    try {
      await startOnboarding({ domain, collection, sample });
      setDomain("");
      reloadAll();
    } catch (err) {
      setStartError(err);
    } finally {
      setBusy(false);
    }
  };

  if (!mayRead) {
    return (
      <main className="mx-auto max-w-4xl px-4 py-6">
        <Panel title="Onboarding">
          <Denied capability={Cap.SCHEMA_READ} />
        </Panel>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-4xl px-4 py-6">
      <header className="mb-5">
        <h1 className="font-serif text-2xl text-ink-900">Domain onboarding</h1>
        <p className="mt-1 text-[13px] leading-relaxed text-ink-500">
          Drafting samples the corpus and synthesises a taxonomy, then produces
          the instance table and predicate map that relation extraction reads.
          Without those a graph builds <strong>zero edges</strong> while
          answering exactly like a populated one — so the state of those
          artifacts is shown on every session rather than discovered later.
        </p>
      </header>

      <div className="space-y-4">
        <Panel
          title="Live taxonomies"
          subtitle="One published session per domain. Publishing drops cached graphs — a reload is not enough, because the corpus fingerprint is unchanged."
        >
          {domains.loading ? (
            <Spinner />
          ) : domains.error ? (
            <ErrorNote error={domains.error} />
          ) : !domains.data || domains.data.domains.length === 0 ? (
            <Empty>No domains onboarded yet.</Empty>
          ) : (
            <ul className="space-y-1.5">
              {domains.data.domains.map((d) => (
                <li
                  key={d.domain}
                  className="flex flex-wrap items-center gap-2 rounded-xl border border-cream-300 px-2.5 py-2"
                >
                  <code className="font-mono text-[13px] text-ink-900">{d.domain}</code>
                  {d.published ? (
                    <>
                      <span className="chip bg-ok/10 text-ok">published</span>
                      <RelationsBadge available={d.published.relations_available} />
                      <span className="tnum ml-auto text-[11px] text-ink-400">
                        {d.published.collection} ·{" "}
                        {d.published.stats?.instances ?? "—"} instances ·{" "}
                        {d.published.stats?.predicates ?? "—"} predicates
                      </span>
                    </>
                  ) : (
                    <span className="chip bg-ink-400/10 text-ink-500">
                      {d.sessions} session{d.sessions === 1 ? "" : "s"}, none published
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel
          title="Draft a taxonomy"
          subtitle="Runs as a queued workload, not in the request. Every LLM call is attributed to this tenant and refused if it is over its ceiling."
        >
          {!mayAuthor ? (
            <Denied capability={Cap.SCHEMA_AUTHOR} />
          ) : (
            <form onSubmit={start}>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <label>
                  <span className="label">Domain</span>
                  <input
                    value={domain}
                    onChange={(e) => setDomain(e.target.value)}
                    required
                    pattern="[a-z0-9][a-z0-9_-]*"
                    placeholder="kgdemo"
                    title="lowercase letters, digits, underscore and hyphen"
                    className="input font-mono"
                  />
                </label>
                <label>
                  <span className="label">Collection</span>
                  <input
                    value={collection}
                    onChange={(e) => setCollection(e.target.value)}
                    required
                    className="input font-mono"
                  />
                </label>
                <label>
                  <span className="label">Chunks to sample</span>
                  <input
                    type="number"
                    min={1}
                    max={1000}
                    value={sample}
                    onChange={(e) => setSample(Number(e.target.value))}
                    className="input tnum"
                  />
                </label>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <button type="submit" disabled={busy || !domain} className="btn-primary">
                  {busy ? "Queueing…" : "Start draft"}
                </button>
                <span className="text-[11px] text-ink-400">
                  One extraction call per sampled chunk, plus a fixed synthesis
                  pass. Cost scales with this number.
                </span>
              </div>
              {startError ? (
                <div className="mt-2">
                  <ErrorNote error={startError} />
                  {startError instanceof ApiError && startError.status === 409 ? (
                    <p className="mt-1 text-[11.5px] text-ink-500">
                      Two concurrent drafts of one domain would each spend a full
                      corpus of extraction calls to produce competing taxonomies.
                    </p>
                  ) : null}
                </div>
              ) : null}
            </form>
          )}
        </Panel>

        <Panel
          title="Sessions"
          actions={
            <button type="button" onClick={reloadAll} className="btn-ghost">
              Refresh
            </button>
          }
        >
          {sessions.loading ? (
            <Spinner />
          ) : sessions.error ? (
            <ErrorNote error={sessions.error} />
          ) : !sessions.data || sessions.data.sessions.length === 0 ? (
            <Empty>No onboarding sessions yet.</Empty>
          ) : (
            <div className="space-y-2">
              {sessions.data.sessions.map((s) => (
                <Detail key={s.id} session={s} me={me} onChanged={reloadAll} />
              ))}
            </div>
          )}
        </Panel>
      </div>
    </main>
  );
}

export default function Page() {
  return <Shell>{(me) => <Onboard me={me} />}</Shell>;
}


/**
 * The taxonomy, editable before approval.
 *
 * Read-only until now, which made the common review outcome — "this is right in
 * shape and too coarse in detail" — unactionable: the only remedy was a full
 * re-draft at full cost. Measured on 2026-08-14, a drafted schema declared three
 * entity types for a twelve-type corpus and the published graph admitted 1 of
 * 306 candidate edges.
 *
 * Two things make the edit real rather than cosmetic:
 *
 * 1. **The fit report.** An entity the schema does not declare is typed `raw:`
 *    by the classifier, and edge validation requires both endpoints to be
 *    declared — so those relations are discarded at build time. The report names
 *    exactly which types are missing and how many instances each covers.
 * 2. **Retyping.** Declaring `Alarm` changes nothing on its own: the instance
 *    table still types those entities `raw:alarm`. The checkboxes below map the
 *    free-form types onto declared ones in the same act, because a schema edit
 *    without them looks like a fix and is not one.
 */
function SchemaEditor({
  session,
  full,
  me,
  busy,
  onSaved,
}: {
  session: OnboardingSession;
  full: OnboardingSession;
  me: Me;
  busy: boolean;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState(full.schema_yaml || "");
  const [picked, setPicked] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const editable =
    session.status === "draft_ready" && can(me, Cap.SCHEMA_AUTHOR);
  const fit = full.taxonomy_fit;
  const dirty = draft !== (full.schema_yaml || "");

  const save = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const result = await editSchema(session.id, draft, picked);
      setSaved(
        `Saved. ${result.instances_retyped} instance(s) retyped; ` +
          `${result.taxonomy_fit.instances_unclassified} still unclassified.`,
      );
      setPicked({});
      onSaved();
    } catch (err) {
      setSaveError(err);
    } finally {
      setSaving(false);
    }
  };

  return (
    <details className="mt-3" open={!!fit && fit.unclassified_share >= 0.25}>
      <summary className="cursor-pointer text-[12px] font-medium text-ink-600">
        Taxonomy{" "}
        {full.schema_edited_at ? (
          <span className="text-ink-400">· edited by a human</span>
        ) : null}
      </summary>

      {full.schema_error ? (
        <p className="mt-1.5 rounded-lg border border-danger/30 bg-danger/5 px-2 py-1.5 text-[12px] text-danger">
          The stored taxonomy no longer parses: {full.schema_error}
        </p>
      ) : null}

      {fit ? (
        <div
          className={`mt-1.5 rounded-lg border px-2.5 py-2 text-[12px] ${
            fit.instances_unclassified
              ? "border-warn/30 bg-warn/5"
              : "border-ok/30 bg-ok/5"
          }`}
        >
          <div className="font-semibold text-ink-800">
            {fit.instances_unclassified
              ? `${fit.instances_unclassified} of ${fit.instances} entities are not covered by this taxonomy`
              : `All ${fit.instances} entities are covered`}
          </div>
          {fit.instances_unclassified ? (
            <>
              <p className="mt-1 leading-relaxed text-ink-600">
                An entity the schema does not declare keeps a{" "}
                <code className="font-mono">raw:</code> type, and an edge is
                admitted only when <em>both</em> endpoints are declared types —
                so every relation touching one is dropped when the graph is
                built. Declare the types below and tick them to retype the
                instances in the same edit; declaring alone changes nothing.
              </p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {fit.suggested_entity_types.map((sug) => {
                  const target = picked[sug.type];
                  return (
                    <label
                      key={sug.type}
                      className={`chip cursor-pointer gap-1 ${
                        target ? "bg-copper-600 text-white" : "bg-ink-900/5 text-ink-600"
                      }`}
                      title={
                        target
                          ? `will be retyped to ${target}`
                          : "tick once the taxonomy declares a matching entity type"
                      }
                    >
                      <input
                        type="checkbox"
                        className="h-3 w-3"
                        disabled={!editable}
                        checked={!!target}
                        onChange={(e) => {
                          const next = { ...picked };
                          if (e.target.checked) {
                            // Match a declared type case-insensitively, else use
                            // the free-form name as written — the server refuses
                            // a target the taxonomy does not declare, which is
                            // the check that keeps this honest.
                            const declared =
                              full.schema?.entity_types.find(
                                (t) =>
                                  t.toLowerCase() ===
                                  sug.type.replace(/\s+/g, "").toLowerCase(),
                              ) || sug.type;
                            next[sug.type] = declared;
                          } else {
                            delete next[sug.type];
                          }
                          setPicked(next);
                        }}
                      />
                      {sug.type}
                      <span className="tnum opacity-70">{sug.instances}</span>
                    </label>
                  );
                })}
              </div>
            </>
          ) : null}
        </div>
      ) : null}

      <textarea
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        readOnly={!editable}
        rows={18}
        spellCheck={false}
        className="mt-1.5 w-full rounded-lg border border-cream-300 bg-cream-100 p-2 font-mono text-[11px] leading-relaxed text-ink-700 focus:border-copper-400 focus:outline-none read-only:opacity-90"
      />

      {saveError ? (
        <div className="mt-1.5">
          <ErrorNote error={saveError} />
        </div>
      ) : null}
      {saved ? (
        <p className="mt-1.5 text-[12px] text-ink-500">{saved}</p>
      ) : null}

      {editable ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy || saving || (!dirty && !Object.keys(picked).length)}
            onClick={() => void save()}
            className="btn-primary px-3 py-1 text-[12px]"
          >
            {saving ? "Saving…" : "Save taxonomy"}
          </button>
          <span className="text-[11px] leading-relaxed text-ink-400">
            Saving makes you its author, so a different principal has to approve
            it — the same rule that stops you approving your own draft.
          </span>
        </div>
      ) : session.status === "draft_ready" ? (
        <p className="mt-1.5 text-[11px] text-ink-400">
          Requires <code className="font-mono">{Cap.SCHEMA_AUTHOR}</code> to
          edit. Writing the taxonomy is authoring it, whoever does it.
        </p>
      ) : (
        <p className="mt-1.5 text-[11px] text-ink-400">
          Frozen — a {session.status} taxonomy cannot be edited, or the published
          content would not be the content anyone approved.
        </p>
      )}
    </details>
  );
}
