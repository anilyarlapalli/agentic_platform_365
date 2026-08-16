"use client";

import type { Me } from "@/lib/api";
import { Shell } from "@/components/Shell";
import { ApprovalsPanel } from "@/components/admin/ApprovalsPanel";
import { DocumentsPanel } from "@/components/admin/DocumentsPanel";
import { EvalPanel } from "@/components/admin/EvalPanel";
import { MembersPanel } from "@/components/admin/MembersPanel";
import { RunsPanel } from "@/components/admin/RunsPanel";
import { UsagePanel } from "@/components/admin/UsagePanel";

function Admin({ me }: { me: Me }) {
  return (
    <main className="mx-auto max-w-6xl px-4 py-6">
      <header className="mb-5">
        <h1 className="font-serif text-2xl text-ink-900">Administration</h1>
        <p className="mt-1 max-w-3xl text-[13px] leading-relaxed text-ink-500">
          Every panel below is gated on a capability rather than a role. Panels
          you cannot use are shown locked instead of hidden — what you may not do
          is as much a part of understanding the policy as what you may. Nothing
          here is the enforcement point: the API checks each request
          independently.
        </p>
        <div className="mt-2 flex flex-wrap gap-1">
          {me.capabilities.map((c) => (
            <span key={c} className="chip bg-ink-900/5 font-mono text-ink-500">
              {c}
            </span>
          ))}
        </div>
      </header>

      <div className="space-y-4">
        <UsagePanel me={me} />
        <ApprovalsPanel me={me} />
        <RunsPanel me={me} />
        <DocumentsPanel me={me} />
        <EvalPanel me={me} />
        <MembersPanel me={me} />
      </div>
    </main>
  );
}

export default function Page() {
  return <Shell>{(me) => <Admin me={me} />}</Shell>;
}
