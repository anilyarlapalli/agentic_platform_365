"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Cap, can, logout, type Me } from "@/lib/api";

type Props = {
  me: Me;
  onSignOut: () => void;
};

export function Nav({ me, onSignOut }: Props) {
  const pathname = usePathname();

  // Nav entries are capability-gated so the console does not advertise pages
  // whose every call would 403. This is presentation only — the server checks
  // each request regardless, so a user who types the URL is still refused.
  const links = [
    { href: "/", label: "Chat", show: can(me, Cap.QUERY_READ) },
    { href: "/admin", label: "Admin", show: true },
    {
      href: "/admin/onboard",
      label: "Onboard",
      show: can(me, Cap.SCHEMA_READ),
    },
  ].filter((l) => l.show);

  return (
    <header className="sticky top-0 z-20 border-b border-cream-300 bg-cream-50/90 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center gap-4 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-copper-600 text-xs font-bold text-cream-50">
            lp
          </div>
          <span className="font-serif text-[15px] text-ink-900">local-platform</span>
        </div>

        <nav className="flex items-center gap-1">
          {links.map((l) => {
            const active =
              l.href === "/" ? pathname === "/" : pathname.startsWith(l.href);
            return (
              <Link
                key={l.href}
                href={l.href}
                className={`rounded-lg px-2.5 py-1 text-[13px] font-medium transition ${
                  active
                    ? "bg-cream-200 text-ink-900"
                    : "text-ink-600 hover:bg-cream-100"
                }`}
              >
                {l.label}
              </Link>
            );
          })}
        </nav>

        <div className="ml-auto flex items-center gap-3">
          <div className="text-right leading-tight">
            <div className="font-mono text-[12px] text-ink-800">{me.tenant}</div>
            <div className="text-[11px] text-ink-400">
              {me.subject} · {me.roles.join(", ") || "no roles"}
            </div>
          </div>
          <button
            type="button"
            onClick={() => {
              void logout().finally(onSignOut);
            }}
            className="btn-ghost px-2.5 py-1 text-[12px]"
          >
            Sign out
          </button>
        </div>
      </div>
    </header>
  );
}
