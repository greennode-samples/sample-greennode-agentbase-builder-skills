// OIDC Authorization Code + PKCE (expo-auth-session). Tokens stored in SecureStore (native).
// Backend verifies the JWT with the same issuer's JWKS — user_id = claim `sub`.
import * as AuthSession from 'expo-auth-session';
import * as SecureStore from 'expo-secure-store';
import * as WebBrowser from 'expo-web-browser';
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { Platform } from 'react-native';

import { env } from '../config/env';

WebBrowser.maybeCompleteAuthSession();

// idToken: only used as id_token_hint when ending the IdP session at sign-out (never sent to the agent)
type Tokens = { accessToken: string; refreshToken?: string; idToken?: string; expiresAt: number };
type AuthState = {
  ready: boolean;
  signedIn: boolean;
  userId: string | null;
  /** Throws a readable Error when the IdP returns an error; resolves silently when the user cancels. */
  signIn: () => Promise<void>;
  /**
   * User sign-out: forget the tokens, revoke the refresh token (best effort) and end the IdP session
   * (end_session_endpoint + id_token_hint). `{ local: true }`: only forget the tokens (401 / rejected refresh token).
   */
  signOut: (opts?: { local?: boolean }) => Promise<void>;
  /**
   * Rejects on a transient refresh failure (offline, IdP down) — the session is kept, retry later.
   * `forceRefresh`: refresh even if the token is not expired yet (the agent answered 401) — null if that fails.
   */
  getAccessToken: (opts?: { forceRefresh?: boolean }) => Promise<string | null>;
};

const KEY = 'agentbase-build.tokens';
const LOCAL_USER = 'local-user';
const AuthContext = createContext<AuthState | null>(null);

const store = {
  async get(): Promise<Tokens | null> {
    if (Platform.OS === 'web') return null; // web: kept in memory only
    const raw = await SecureStore.getItemAsync(KEY);
    return raw ? (JSON.parse(raw) as Tokens) : null;
  },
  async set(t: Tokens | null) {
    if (Platform.OS === 'web') return;
    if (t) await SecureStore.setItemAsync(KEY, JSON.stringify(t));
    else await SecureStore.deleteItemAsync(KEY);
  },
};

function decodeSub(jwt: string): string | null {
  try {
    const payload = jwt.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(globalThis.atob(payload)).sub ?? null;
  } catch {
    return null;
  }
}

const query = (params: Record<string, string | undefined>) =>
  Object.entries(params)
    .filter(([, v]) => v)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v!)}`)
    .join('&');

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [tokens, setTokensState] = useState<Tokens | null>(null);
  // Always-current copy for async code: a getAccessToken captured before a refresh must not reuse the old
  // (rotated, now revoked) refresh token — that would fail with invalid_grant and sign the user out.
  const tokensRef = useRef<Tokens | null>(null);
  const [ready, setReady] = useState(false);
  const [discovery, setDiscovery] = useState<AuthSession.DiscoveryDocument | null>(null);
  const discoveryRef = useRef<AuthSession.DiscoveryDocument | null>(null);
  // Must be registered at the IdP EXACTLY — it depends on how the app runs (see SKILL.md):
  // dev/store build `<scheme>://auth` · Expo Go `exp://<LAN-IP>:8081/--/auth` · web `http://localhost:8081/auth`
  // Also used as post_logout_redirect_uri (register it as an allowed post-logout redirect too).
  const redirectUri = AuthSession.makeRedirectUri({ path: 'auth' });
  const [request, , promptAsync] = AuthSession.useAuthRequest(
    {
      clientId: env.oidc.clientId,
      scopes: env.oidc.scopes,
      redirectUri,
      usePKCE: true,
      extraParams: env.oidc.audience ? { audience: env.oidc.audience } : undefined,
    },
    discovery,
  );

  useEffect(() => {
    if (__DEV__ && env.authMode === 'jwt') console.log('[auth] redirect_uri to register at the IdP:', redirectUri);
  }, [redirectUri]);

  const setTokens = useCallback(async (t: Tokens | null) => {
    tokensRef.current = t;
    setTokensState(t);
    await store.set(t);
  }, []);

  useEffect(() => {
    store
      .get()
      .catch(() => null)
      .then((t) => {
        if (!tokensRef.current) {
          tokensRef.current = t;
          setTokensState(t);
        }
        setReady(true);
      });
  }, []);

  // Discovery only in jwt mode (`none` has no IdP — fetching a placeholder issuer was an unhandled rejection).
  // A failure (offline at startup) is retried on the next sign-in / refresh instead of crashing.
  const loadDiscovery = useCallback(async () => {
    if (discoveryRef.current) return discoveryRef.current;
    const d = await AuthSession.fetchDiscoveryAsync(env.oidc.issuer);
    discoveryRef.current = d;
    setDiscovery(d);
    return d;
  }, []);

  useEffect(() => {
    if (env.authMode === 'none') return;
    if (!env.oidc.issuer) {
      console.warn('[auth] EXPO_PUBLIC_OIDC_ISSUER is empty — sign-in is disabled');
      return;
    }
    loadDiscovery().catch((e: unknown) => console.warn('[auth] OIDC discovery failed, will retry:', String(e)));
  }, [loadDiscovery]);

  const save = useCallback(
    async (r: AuthSession.TokenResponse) => {
      const t: Tokens = {
        accessToken: r.accessToken,
        refreshToken: r.refreshToken ?? tokensRef.current?.refreshToken, // IdPs without rotation omit it
        idToken: r.idToken ?? tokensRef.current?.idToken,
        expiresAt: Date.now() + (r.expiresIn ?? 300) * 1000,
      };
      await setTokens(t);
      return t;
    },
    [setTokens],
  );

  const signIn = useCallback(async () => {
    if (env.authMode === 'none') return;
    const disc = discoveryRef.current;
    if (!request || !disc) {
      if (env.oidc.issuer) loadDiscovery().catch(() => undefined); // offline at startup ⇒ retry for the next tap
      throw new Error(
        env.oidc.issuer
          ? 'Sign-in is not ready (identity provider unreachable?) — try again in a moment'
          : 'Sign-in is not configured (EXPO_PUBLIC_OIDC_ISSUER is empty)',
      );
    }
    const res = await promptAsync({ preferEphemeralSession: env.oidc.ephemeralSession });
    if (res.type === 'error') {
      // e.g. invalid redirect_uri / unauthorized_client / access_denied — tell the user instead of doing nothing
      const e = res.error;
      throw new Error(
        `Sign-in failed: ${e?.description || res.params?.error_description || e?.code || res.params?.error || 'unknown error'}`,
      );
    }
    if (res.type !== 'success') return; // cancelled / dismissed by the user
    const tokenRes = await AuthSession.exchangeCodeAsync(
      {
        clientId: env.oidc.clientId,
        code: res.params.code,
        redirectUri,
        extraParams: { code_verifier: request.codeVerifier ?? '' },
      },
      disc,
    );
    await save(tokenRes);
  }, [request, promptAsync, redirectUri, save, loadDiscovery]);

  const signOut = useCallback(
    async (opts?: { local?: boolean }) => {
      const t = tokensRef.current;
      await setTokens(null); // locally signed out first — even if the IdP is unreachable below
      if (opts?.local || !t || env.authMode === 'none') return;
      let disc: AuthSession.DiscoveryDocument;
      try {
        disc = await loadDiscovery();
      } catch {
        return; // offline: local sign-out only
      }
      // 1) Revoke the refresh token (RFC 7009) so a copy of it can't mint new access tokens
      if (t.refreshToken && disc.revocationEndpoint) {
        await AuthSession.revokeAsync(
          {
            token: t.refreshToken,
            tokenTypeHint: AuthSession.TokenTypeHint.RefreshToken,
            clientId: env.oidc.clientId,
          },
          disc,
        ).catch((e: unknown) => console.warn('[auth] token revocation failed:', String(e)));
      }
      // 2) End the IdP (SSO) session — otherwise the next "Sign in" logs straight back in without credentials
      if (disc.endSessionEndpoint) {
        const url =
          disc.endSessionEndpoint +
          (disc.endSessionEndpoint.includes('?') ? '&' : '?') +
          query({ client_id: env.oidc.clientId, id_token_hint: t.idToken, post_logout_redirect_uri: redirectUri });
        await WebBrowser.openAuthSessionAsync(url, redirectUri, {
          preferEphemeralSession: env.oidc.ephemeralSession,
        }).catch((e: unknown) => console.warn('[auth] end-session failed:', String(e)));
      }
    },
    [setTokens, loadDiscovery, redirectUri],
  );

  const refreshing = useRef<Promise<string | null> | null>(null);

  const getAccessToken = useCallback(
    async (opts?: { forceRefresh?: boolean }): Promise<string | null> => {
      if (env.authMode === 'none') return null;
      const t = tokensRef.current;
      if (!t) return null;
      if (!opts?.forceRefresh && Date.now() < t.expiresAt - 30_000) return t.accessToken;
      if (!t.refreshToken) {
        await signOut({ local: true }); // expired / rejected and nothing to refresh with ⇒ the session is over
        return null;
      }
      // Single-flight: concurrent callers share ONE refresh. Two refreshes with the same refresh token fail under
      // refresh-token rotation and would sign the user out.
      if (!refreshing.current) {
        const refreshToken = t.refreshToken;
        refreshing.current = (async () => {
          try {
            const r = await AuthSession.refreshAsync(
              { clientId: env.oidc.clientId, refreshToken },
              await loadDiscovery(),
            );
            // Signed out (or in again) while refreshing ⇒ don't resurrect the old session
            if (tokensRef.current?.refreshToken !== refreshToken) return tokensRef.current?.accessToken ?? null;
            return (await save(r)).accessToken;
          } catch (e) {
            // The IdP REJECTED the refresh token (OAuth error response, e.g. invalid_grant: expired / revoked) ⇒ sign
            // out. Anything else (offline, timeout, IdP 5xx) is transient: keep the session and let the caller retry.
            if (e instanceof AuthSession.TokenError) {
              await signOut({ local: true });
              return null;
            }
            throw e;
          } finally {
            refreshing.current = null;
          }
        })();
      }
      return refreshing.current;
    },
    [loadDiscovery, save, signOut],
  );

  const value = useMemo<AuthState>(
    () => ({
      ready,
      signedIn: env.authMode === 'none' || !!tokens,
      userId: env.authMode === 'none' ? LOCAL_USER : tokens ? decodeSub(tokens.accessToken) : null,
      signIn,
      signOut,
      getAccessToken,
    }),
    [ready, tokens, signIn, signOut, getAccessToken],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider');
  return ctx;
}
