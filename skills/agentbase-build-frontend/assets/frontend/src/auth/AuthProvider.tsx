// OIDC Authorization Code + PKCE (expo-auth-session). Tokens stored in SecureStore (native).
// Backend verifies the JWT with the same issuer's JWKS — user_id = claim `sub`.
import * as AuthSession from 'expo-auth-session';
import * as SecureStore from 'expo-secure-store';
import * as WebBrowser from 'expo-web-browser';
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { Platform } from 'react-native';

import { env } from '../config/env';

WebBrowser.maybeCompleteAuthSession();

type Tokens = { accessToken: string; refreshToken?: string; expiresAt: number };
type AuthState = {
  ready: boolean;
  signedIn: boolean;
  userId: string | null;
  signIn: () => Promise<void>;
  signOut: () => Promise<void>;
  // Rejects on a transient refresh failure (offline, IdP down) — the session is kept, retry later
  getAccessToken: () => Promise<string | null>;
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
      throw new Error('Sign-in is not ready (identity provider unreachable?) — try again in a moment');
    }
    const res = await promptAsync();
    if (res.type !== 'success') return;
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

  const signOut = useCallback(() => setTokens(null), [setTokens]);

  const refreshing = useRef<Promise<string | null> | null>(null);

  const getAccessToken = useCallback(async (): Promise<string | null> => {
    if (env.authMode === 'none') return null;
    const t = tokensRef.current;
    if (!t) return null;
    if (Date.now() < t.expiresAt - 30_000) return t.accessToken;
    if (!t.refreshToken) {
      await signOut(); // expired and nothing to refresh with ⇒ the session is over
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
          // The IdP REJECTED the refresh token (OAuth error response, e.g. invalid_grant: expired / revoked) ⇒ sign out.
          // Anything else (offline, timeout, IdP 5xx) is transient: keep the session and let the caller retry.
          if (e instanceof AuthSession.TokenError) {
            await signOut();
            return null;
          }
          throw e;
        } finally {
          refreshing.current = null;
        }
      })();
    }
    return refreshing.current;
  }, [loadDiscovery, save, signOut]);

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
