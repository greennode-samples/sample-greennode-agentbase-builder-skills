'use client';
// Browser side of the BFF session: asks GET /api/auth/session who is signed in (+ the CSRF token). Holds NO token —
// sign-in is a full-page navigation to /api/auth/login (OIDC code + PKCE run by the server), sign-out a CSRF-protected
// POST /api/auth/logout followed by a navigation to the IdP's end-session URL.
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

import { useStrings } from '../lib/LocaleProvider';

export type AuthStatus = 'loading' | 'signed_in' | 'signed_out';
type Auth = {
  status: AuthStatus;
  /** the signed-in user's id (`sub`; "local-user" with BFF_AUTH_MODE=none) — owner of the conversation */
  userId: string | null;
  userName: string | null;
  csrfToken: string | null;
  /** sign-in / session error to show on the sign-in screen */
  error: string | null;
  signIn: () => void;
  signOut: () => Promise<void>;
  /** the BFF answered 401 (refresh token rejected): back to the sign-in screen */
  expired: () => void;
};

const AuthContext = createContext<Auth | null>(null);

// Codes set by /api/auth/callback (never free text from the URL): the texts are in src/lib/i18n.ts

type SessionInfo = { authenticated: boolean; user?: { id: string; name: string | null }; csrfToken?: string };

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const t = useStrings();
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [info, setInfo] = useState<SessionInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const url = new URL(window.location.href);
    const code = url.searchParams.get('auth_error');
    if (code) {
      setError(t.authErrors[code] ?? t.authErrors.login_failed ?? '');
      url.searchParams.delete('auth_error');
      window.history.replaceState(null, '', url.pathname + url.search);
    }
    let cancelled = false;
    fetch('/api/auth/session', { cache: 'no-store', credentials: 'same-origin' })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return (await r.json()) as SessionInfo;
      })
      .then((s) => {
        if (cancelled) return;
        setInfo(s);
        setStatus(s.authenticated ? 'signed_in' : 'signed_out');
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(t.serverUnreachable(e instanceof Error ? e.message : String(e)));
        setStatus('signed_out');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const signIn = useCallback(() => window.location.assign('/api/auth/login'), []);

  const signOut = useCallback(async () => {
    let redirect = '/';
    try {
      const r = await fetch('/api/auth/logout', {
        method: 'POST',
        credentials: 'same-origin',
        headers: info?.csrfToken ? { 'X-CSRF-Token': info.csrfToken } : {},
      });
      const body = (await r.json()) as { redirect?: unknown };
      // only our own path or an https IdP URL (the BFF builds it from the IdP discovery)
      if (typeof body.redirect === 'string' && /^(\/(?!\/)|https:\/\/)/.test(body.redirect)) redirect = body.redirect;
    } catch {}
    setInfo(null);
    setStatus('signed_out');
    window.location.assign(redirect);
  }, [info]);

  const expired = useCallback(() => {
    setInfo(null);
    setError('Your session has expired — sign in again.');
    setStatus('signed_out');
  }, []);

  const value = useMemo<Auth>(
    () => ({
      status,
      userId: info?.user?.id ?? null,
      userName: info?.user?.name ?? null,
      csrfToken: info?.csrfToken ?? null,
      error,
      signIn,
      signOut,
      expired,
    }),
    [status, info, error, signIn, signOut, expired],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): Auth {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider');
  return ctx;
}
