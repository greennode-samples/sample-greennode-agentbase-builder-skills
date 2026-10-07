// OIDC Authorization Code + PKCE (expo-auth-session). Tokens stored in SecureStore (native).
// Backend verifies the JWT with the same issuer's JWKS — user_id = claim `sub`.
import * as AuthSession from 'expo-auth-session';
import * as SecureStore from 'expo-secure-store';
import * as WebBrowser from 'expo-web-browser';
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
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
  const [tokens, setTokens] = useState<Tokens | null>(null);
  const [ready, setReady] = useState(false);
  const discovery = AuthSession.useAutoDiscovery(env.oidc.issuer || 'https://invalid.local');
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
    store.get().then((t) => {
      setTokens(t);
      setReady(true);
    });
  }, []);

  const save = useCallback(async (r: AuthSession.TokenResponse) => {
    const t: Tokens = {
      accessToken: r.accessToken,
      refreshToken: r.refreshToken ?? tokens?.refreshToken,
      expiresAt: Date.now() + (r.expiresIn ?? 300) * 1000,
    };
    setTokens(t);
    await store.set(t);
    return t;
  }, [tokens?.refreshToken]);

  const signIn = useCallback(async () => {
    if (env.authMode === 'none') return;
    if (!request || !discovery) throw new Error('OIDC not ready');
    const res = await promptAsync();
    if (res.type !== 'success') return;
    const tokenRes = await AuthSession.exchangeCodeAsync(
      {
        clientId: env.oidc.clientId,
        code: res.params.code,
        redirectUri,
        extraParams: { code_verifier: request.codeVerifier ?? '' },
      },
      discovery,
    );
    await save(tokenRes);
  }, [request, discovery, promptAsync, redirectUri, save]);

  const signOut = useCallback(async () => {
    setTokens(null);
    await store.set(null);
  }, []);

  const getAccessToken = useCallback(async () => {
    if (env.authMode === 'none') return null;
    if (!tokens) return null;
    if (Date.now() < tokens.expiresAt - 30_000) return tokens.accessToken;
    if (!tokens.refreshToken || !discovery) {
      await signOut();
      return null;
    }
    try {
      const r = await AuthSession.refreshAsync(
        { clientId: env.oidc.clientId, refreshToken: tokens.refreshToken },
        discovery,
      );
      return (await save(r)).accessToken;
    } catch {
      await signOut();
      return null;
    }
  }, [tokens, discovery, save, signOut]);

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
