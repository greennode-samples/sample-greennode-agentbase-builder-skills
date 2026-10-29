import { afterEach, describe, expect, it, vi } from 'vitest';

import { parseConfig } from '../src/server/config';
import type { Session } from '../src/server/session';
import { freshSession } from '../src/server/tokens';

const cfg = parseConfig(
  {
    BFF_AGENT_URL: 'https://runtime.example/agent',
    BFF_OIDC_ISSUER: 'https://idp.example/realms/r',
    BFF_OIDC_CLIENT_ID: 'web',
    BFF_APP_ORIGIN: 'https://chat.example.com',
    BFF_SESSION_SECRET: 'x'.repeat(48),
  },
  true,
);
const now = () => Math.floor(Date.now() / 1000);
// A refresh token per test: the BFF shares refresh results per refresh token for 30 s (module state)
const session = (refreshToken: string, over: Partial<Session> = {}): Session => ({
  sub: 'u',
  csrf: 'c',
  accessToken: 'at-1',
  accessTokenExp: now() + 3000,
  refreshToken,
  ...over,
});

/** IdP stub: discovery + a token endpoint answering with the next access token of `tokens` (refresh tokens never rotate). */
function idp(tokens: string[]) {
  const calls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.endsWith('/.well-known/openid-configuration')) {
        return Response.json({ issuer: cfg.oidc.issuer, authorization_endpoint: 'https://idp.example/a', token_endpoint: 'https://idp.example/token' });
      }
      const next = tokens[calls.length] ?? 'at-last';
      calls.push(next);
      return Response.json({ access_token: next, expires_in: 300 });
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe('forced refresh after the agent answered 401', () => {
  it('does not hand back the access token that was just rejected', async () => {
    const calls = idp(['at-2', 'at-3']);
    // 1st call of a request: the token is about to expire ⇒ refreshed to at-2 (the IdP keeps rt-1 valid)
    const first = await freshSession(cfg, session('rt-a', { accessTokenExp: now() + 5 }));
    expect(first.session.accessToken).toBe('at-2');
    // the agent rejects at-2 ⇒ forced: a NEW token, not the cached at-2 (it used to be reused for 30 s ⇒ sign-out)
    const forced = await freshSession(cfg, first.session, true);
    expect(forced.session.accessToken).toBe('at-3');
    expect(calls).toEqual(['at-2', 'at-3']);
  });

  it('parallel forced refreshes of the same rejected token share one refresh', async () => {
    const calls = idp(['at-2', 'at-3', 'at-4']);
    const s = (await freshSession(cfg, session('rt-b', { accessTokenExp: now() + 5 }))).session; // at-2
    const [a, b] = await Promise.all([freshSession(cfg, s, true), freshSession(cfg, s, true)]);
    expect(a.session.accessToken).toBe('at-3');
    expect(b.session.accessToken).toBe('at-3');
    expect(calls).toEqual(['at-2', 'at-3']); // not three: the second request reused the new one
  });

  it('still shares one refresh between parallel requests that were not rejected', async () => {
    const calls = idp(['at-2', 'at-3']);
    const s = session('rt-c', { accessTokenExp: now() + 5 });
    const [a, b] = await Promise.all([freshSession(cfg, s), freshSession(cfg, s)]);
    expect([a.session.accessToken, b.session.accessToken]).toEqual(['at-2', 'at-2']);
    expect(calls).toEqual(['at-2']);
  });
});
