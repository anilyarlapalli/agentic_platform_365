"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchMe,
  setUnauthenticatedHandler,
  type Me,
} from "@/lib/api";
import { AuthGate } from "@/components/AuthGate";
import { Nav } from "@/components/Nav";

type Props = {
  children: (me: Me) => React.ReactNode;
};

/**
 * Auth boundary shared by every page.
 *
 * The HttpOnly cookie is never visible to this component. `/auth/me` resolves
 * it against current principal state before any page renders.
 */
export function Shell({ children }: Props) {
  const [me, setMe] = useState<Me | null>(null);
  const [checking, setChecking] = useState(true);

  const resolve = useCallback(async () => {
    try {
      setMe(await fetchMe());
    } catch {
      // Any failure here means "not signed in". The server will overwrite an
      // expired cookie on the next successful login.
      setMe(null);
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    // A 401 from *any* later call drops the whole console back to the gate,
    // rather than leaving a signed-out user staring at a page of failed panels.
    setUnauthenticatedHandler(() => setMe(null));
    void resolve();
    return () => setUnauthenticatedHandler(null);
  }, [resolve]);

  if (checking) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-ink-400">
        Checking session…
      </div>
    );
  }

  if (!me) {
    return (
      <AuthGate
        onSignIn={() => {
          setChecking(true);
          void resolve();
        }}
      />
    );
  }

  return (
    <div className="min-h-screen">
      <Nav me={me} onSignOut={() => setMe(null)} />
      {children(me)}
    </div>
  );
}
