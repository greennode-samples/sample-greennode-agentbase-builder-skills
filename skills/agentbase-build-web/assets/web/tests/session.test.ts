import { describe, expect, it } from 'vitest';

import { parseConfig } from '../src/server/config';
import { cookieNames, joinChunks, MAX_CHUNKS, splitChunks, staleChunks } from '../src/server/cookies';
import { b64urlDecode, b64urlEncode, pkceChallenge, randomToken, seal, timingSafeEqual, unseal } from '../src/server/crypto';
import { csrfViolation } from '../src/server/csrf';
import { idTokenClaims, OidcError } from '../src/server/oidc';

const SECRET = 'x'.repeat(48);
const ORIGIN = 'https://chat.example.com';

describe('sealed cookies', () => {
  it('round-trips and rejects tampering, another secret, another purpose and expiry', async () => {
    const v = await seal({ sub: 'u1', at: 'token' }, SECRET, 'ab_session', 60);
    expect(v).not.toContain('token'); // encrypted, not just signed
    expect(await unseal(v, SECRET, 'ab_session')).toEqual({ sub: 'u1', at: 'token' });
    const tampered = v.slice(0, -2) + (v.endsWith('A') ? 'BB' : 'AA');
    expect(await unseal(tampered, SECRET, 'ab_session')).toBeNull();
    expect(await unseal(v, 'y'.repeat(48), 'ab_session')).toBeNull();
    expect(await unseal(v, SECRET, 'ab_oidc')).toBeNull();
    expect(await unseal(await seal({}, SECRET, 'p', -1), SECRET, 'p')).toBeNull();
    expect(await unseal('garbage', SECRET, 'p')).toBeNull();
  });
  it('chunks large values and finds stale chunks', () => {
    const big = 'a'.repeat(9000);
    const chunks = splitChunks('s', big);
    expect(chunks.map((c) => c.name)).toEqual(['s.0', 's.1', 's.2']);
    const jar = new Map(chunks.map((c) => [c.name, c.value]));
    expect(joinChunks('s', (n) => jar.get(n))).toBe(big);
    expect(joinChunks('s', () => undefined)).toBeNull();
    expect(staleChunks('s', ['s.0', 's.1', 's.2', 's.3', 'other.4'], 2)).toEqual(['s.2', 's.3']);
    expect(MAX_CHUNKS).toBeGreaterThan(1);
  });
  it('uses __Host- cookies on https only', () => {
    expect(cookieNames(true).session).toBe('__Host-ab_session');
    expect(cookieNames(false).session).toBe('ab_session');
  });
});

describe('crypto helpers', () => {
  it('PKCE S256 matches RFC 7636 appendix B', async () => {
    expect(await pkceChallenge('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk')).toBe('E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM');
  });
  it('base64url round-trips; random tokens are unique; constant-time compare', () => {
    const bytes = new Uint8Array([0, 251, 255, 1]);
    expect(Array.from(b64urlDecode(b64urlEncode(bytes)))).toEqual([0, 251, 255, 1]);
    expect(randomToken()).not.toBe(randomToken());
    expect(timingSafeEqual('abc', 'abc')).toBe(true);
    expect(timingSafeEqual('abc', 'abd')).toBe(false);
    expect(timingSafeEqual('abc', 'abcd')).toBe(false);
  });
});

describe('CSRF', () => {
  const ok = { method: 'POST', origin: ORIGIN, secFetchSite: 'same-origin', token: 't0k' };
  it('accepts same-origin POST with the session token', () => {
    expect(csrfViolation(ok, ORIGIN, 't0k')).toBeNull();
    expect(csrfViolation({ ...ok, origin: null }, ORIGIN, 't0k')).toBeNull(); // Sec-Fetch-Site fallback
  });
  it('rejects cross-origin, missing origin info, wrong / missing token, no session', () => {
    expect(csrfViolation({ ...ok, origin: 'https://evil.test' }, ORIGIN, 't0k')).toMatch(/cross-origin/);
    expect(csrfViolation({ ...ok, origin: null, secFetchSite: 'cross-site' }, ORIGIN, 't0k')).toMatch(/Origin/);
    expect(csrfViolation({ ...ok, token: 'nope' }, ORIGIN, 't0k')).toMatch(/token/);
    expect(csrfViolation({ ...ok, token: null }, ORIGIN, 't0k')).toMatch(/token/);
    expect(csrfViolation(ok, ORIGIN, undefined)).toMatch(/session/);
  });
  it('does not apply to GET', () => {
    expect(csrfViolation({ ...ok, method: 'GET', origin: 'https://evil.test', token: null }, ORIGIN, undefined)).toBeNull();
  });
});

describe('BFF config', () => {
  const prod = {
    BFF_AGENT_URL: 'https://runtime.example/agent',
    BFF_OIDC_ISSUER: 'https://idp.example/realms/r',
    BFF_OIDC_CLIENT_ID: 'web',
    BFF_APP_ORIGIN: ORIGIN + '/',
    BFF_SESSION_SECRET: SECRET,
  };
  it('accepts a complete production config', () => {
    const c = parseConfig(prod, true);
    expect(c).toMatchObject({ appOrigin: ORIGIN, secure: true, authMode: 'jwt', runtimeAuth: 'jwt' });
  });
  it('refuses unsafe production values', () => {
    expect(() => parseConfig({ ...prod, BFF_AUTH_MODE: 'none' }, true)).toThrow(/local development only/);
    expect(() => parseConfig({ ...prod, BFF_SESSION_SECRET: 'short' }, true)).toThrow(/BFF_SESSION_SECRET/);
    expect(() => parseConfig({ ...prod, BFF_APP_ORIGIN: 'http://chat.example.com' }, true)).toThrow(/BFF_APP_ORIGIN/);
    expect(() => parseConfig({ ...prod, BFF_AGENT_URL: 'http://runtime.example' }, true)).toThrow(/BFF_AGENT_URL/);
    expect(() => parseConfig({ ...prod, BFF_RUNTIME_AUTH: 'iam' }, true)).toThrow(/BFF_IAM_CLIENT_ID/);
  });
  it('allows http + none in development', () => {
    expect(parseConfig({ BFF_AUTH_MODE: 'none' }, false, 'dev-secret')).toMatchObject({ secure: false, authMode: 'none' });
  });
});

describe('ID token claims', () => {
  const jwt = (claims: object) => `h.${b64urlEncode(new TextEncoder().encode(JSON.stringify(claims)))}.s`;
  const base = { iss: 'https://idp/', aud: 'web', nonce: 'n1', sub: 'u1', exp: Date.now() / 1000 + 60, name: 'Ann' };
  const expect_ = { issuer: 'https://idp', clientId: 'web', nonce: 'n1' };
  it('accepts matching iss / aud / nonce', () => {
    expect(idTokenClaims(jwt(base), expect_)).toEqual({ sub: 'u1', name: 'Ann' });
    expect(idTokenClaims(jwt({ ...base, aud: ['x', 'web'] }), expect_).sub).toBe('u1');
  });
  it('rejects a wrong nonce, audience, issuer or an expired token', () => {
    for (const bad of [{ nonce: 'n2' }, { aud: 'other' }, { iss: 'https://evil' }, { exp: 1 }, { sub: '' }]) {
      expect(() => idTokenClaims(jwt({ ...base, ...bad }), expect_)).toThrow(OidcError);
    }
  });
});
