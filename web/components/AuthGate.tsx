"use client";

import { useState } from "react";
import { login, type LoginResponse } from "@/lib/api";

type Props = {
  onSignIn: (res: LoginResponse) => void;
};

/**
 * Demo principals seeded by `make seed-chat`.
 *
 * These two tenants index *contradictory* torque figures for the same spindle
 * on purpose: ask both the same question and a broken tenant boundary shows
 * itself as a wrong answer rather than hiding. Signing in as each in turn is
 * the fastest manual check that isolation holds.
 */
const DEMO = [
  {
    tenant: "demo-acme",
    subject: "operator@acme.example",
    password: "demo-password-1234",
    tagline: "Operator · Acme Industrial",
  },
  {
    tenant: "demo-globex",
    subject: "operator@globex.example",
    password: "demo-password-1234",
    tagline: "Operator · Globex Motors — contradicts Acme by design",
  },
];

export function AuthGate({ onSignIn }: Props) {
  const [tenant, setTenant] = useState(DEMO[0].tenant);
  const [subject, setSubject] = useState(DEMO[0].subject);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      const res = await login(tenant, subject, password);
      onSignIn(res);
    } catch (err) {
      // The API returns one identical 401 for every failure — unknown tenant,
      // unknown subject and wrong password are indistinguishable on purpose, so
      // there is nothing more specific to show here and we must not invent it.
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen w-full items-center justify-center bg-cream-50 px-4 py-10">
      <div className="grid w-full max-w-4xl grid-cols-1 gap-6 md:grid-cols-2">
        <form onSubmit={submit} className="card p-6">
          <div className="mb-1 flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-copper-600 text-sm font-bold text-cream-50">
              lp
            </div>
            <span className="font-serif text-lg text-ink-900">local-platform</span>
          </div>

          <h2 className="mt-3 font-serif text-2xl text-ink-900">Sign in</h2>
          <p className="mt-1 text-[13px] text-ink-500">
            The tenant is part of the credential, not a choice made after
            sign-in. Identity is the pair.
          </p>

          <div className="mt-5 space-y-3">
            <div>
              <span className="label">Tenant</span>
              <input
                value={tenant}
                onChange={(e) => setTenant(e.target.value)}
                autoComplete="organization"
                required
                className="input font-mono"
              />
            </div>
            <div>
              <span className="label">Subject</span>
              <input
                value={subject}
                onChange={(e) => setSubject(e.target.value)}
                autoComplete="username"
                required
                className="input font-mono"
              />
            </div>
            <div>
              <span className="label">Password</span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
                className="input"
              />
            </div>

            {error ? (
              <div className="rounded-xl border border-danger/30 bg-danger/5 px-3 py-2 text-[12.5px] text-danger">
                {error}
              </div>
            ) : null}

            <button
              type="submit"
              disabled={busy || !tenant || !subject || !password}
              className="btn-primary w-full"
            >
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </div>
        </form>

        <div className="card bg-cream-100/60 p-6">
          <h3 className="font-serif text-lg text-ink-900">Demo tenants</h3>
          <p className="mt-1 text-[12.5px] text-ink-500">
            Created by <code className="font-mono">make seed-chat</code>. Click
            to fill the form.
          </p>
          <div className="mt-4 space-y-2">
            {DEMO.map((d) => (
              <button
                key={d.tenant}
                type="button"
                onClick={() => {
                  setTenant(d.tenant);
                  setSubject(d.subject);
                  setPassword(d.password);
                  setError(null);
                }}
                className="w-full rounded-xl border border-cream-300 bg-white px-3 py-2 text-left transition hover:border-copper-400/50 hover:bg-cream-50"
              >
                <code className="font-mono text-[12px] text-ink-800">
                  {d.tenant}
                </code>
                <div className="mt-0.5 text-[11.5px] text-ink-500">
                  {d.tagline}
                </div>
              </button>
            ))}
          </div>
          <p className="mt-4 text-[11px] leading-relaxed text-ink-400">
            Seeded passwords are weak by design and the tenants are{" "}
            <code className="font-mono">demo-</code> prefixed so the test
            fixtures&rsquo; cleanup cannot delete this corpus. Both are local
            only.
          </p>
        </div>
      </div>
    </div>
  );
}
