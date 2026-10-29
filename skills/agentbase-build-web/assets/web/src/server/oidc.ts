// OIDC Authorization Code + PKCE, run by the BFF (server only). The BFF is the OIDC client: it may be confidential
// (BFF_OIDC_CLIENT_SECRET) — tokens are exchanged server-to-server and never reach the browser.
import type { BffConfig } from './config';
import { b64urlDecode } from './crypto';

export type Discovery = {
  issuer: string;
  authorization_endpoint: string;
  token_endpoint: string;
  end_session_endpoint?: string;
  revocation_endpoint?: string;
  token_endpoint_auth_methods_supported?: string[];
};

export type TokenSet = {
  access_token: string;
  expires_in?: number;
  refresh_token?: string;
  id_token?: string;
};

/** invalid_grant: the IdP rejected the code / refresh token (⇒ sign in again) · unavailable: IdP down / 5xx */
export class OidcError extends Error {
  constructor(
    public kind: 'invalid_grant' | 'unavailable' | 'invalid',
    message: string,
  ) {
    super(message);
  }
}

const TIMEOUT_MS = 10_000;
let disco: { issuer: string; at: number; value: Promise<Discovery> } | undefined;

export function discovery(cfg: BffConfig): Promise<Discovery> {
  const { issuer } = cfg.oidc;
  if (disco && disco.issuer === issuer && Date.now() - disco.at < 3600_000) return disco.value;
  const value = (async () => {
    let res: Response;
    try {
      res = await fetch(`${issuer}/.well-known/openid-configuration`, { signal: AbortSignal.timeout(TIMEOUT_MS) });
    } catch (e) {
      throw new OidcError('unavailable', `IdP discovery unreachable: ${String(e)}`);
    }
    if (!res.ok) throw new OidcError('unavailable', `IdP discovery HTTP ${res.status}`);
    const d = (await res.json()) as Discovery;
    // The issuer must match byte for byte (minus a trailing "/", which Auth0 includes): a mismatch means the wrong
    // URL — or a hostile document
    if (d.issuer.replace(/\/+$/, '') !== issuer) throw new OidcError('invalid', `issuer mismatch: ${d.issuer}`);
    return d;
  })();
  disco = { issuer, at: Date.now(), value };
  value.catch(() => {
    if (disco?.value === value) disco = undefined; // retry next time
  });
  return value;
}

export const redirectUri = (cfg: BffConfig) => `${cfg.appOrigin}/api/auth/callback`;

export function authorizationUrl(
  cfg: BffConfig,
  d: Discovery,
  p: { state: string; nonce: string; challenge: string },
): string {
  const u = new URL(d.authorization_endpoint);
  const q = u.searchParams;
  q.set('response_type', 'code');
  q.set('client_id', cfg.oidc.clientId);
  q.set('redirect_uri', redirectUri(cfg));
  q.set('scope', cfg.oidc.scopes);
  q.set('state', p.state);
  q.set('nonce', p.nonce);
  q.set('code_challenge', p.challenge);
  q.set('code_challenge_method', 'S256');
  if (cfg.oidc.audience) q.set('audience', cfg.oidc.audience); // Auth0: else the access token is opaque
  return u.toString();
}

/** Client authentication at the token / revocation endpoints: none (public client), Basic or POST body secret. */
function clientAuth(cfg: BffConfig, d: Discovery, form: URLSearchParams): Record<string, string> {
  const { clientId, clientSecret } = cfg.oidc;
  if (!clientSecret) {
    form.set('client_id', clientId);
    return {};
  }
  const methods = d.token_endpoint_auth_methods_supported ?? ['client_secret_basic'];
  if (!methods.includes('client_secret_basic') && methods.includes('client_secret_post')) {
    form.set('client_id', clientId);
    form.set('client_secret', clientSecret);
    return {};
  }
  const basic = btoa(`${encodeURIComponent(clientId)}:${encodeURIComponent(clientSecret)}`); // RFC 6749 §2.3.1
  return { Authorization: `Basic ${basic}` };
}

async function tokenRequest(cfg: BffConfig, form: URLSearchParams): Promise<TokenSet> {
  const d = await discovery(cfg);
  const headers = { 'Content-Type': 'application/x-www-form-urlencoded', ...clientAuth(cfg, d, form) };
  let res: Response;
  try {
    res = await fetch(d.token_endpoint, { method: 'POST', headers, body: form, signal: AbortSignal.timeout(TIMEOUT_MS) });
  } catch (e) {
    throw new OidcError('unavailable', `token endpoint unreachable: ${String(e)}`);
  }
  const body = (await res.json().catch(() => ({}))) as TokenSet & { error?: string };
  if (res.ok && body.access_token) return body;
  if (res.status >= 500) throw new OidcError('unavailable', `token endpoint HTTP ${res.status}`);
  // 400/401 invalid_grant / invalid_client …: the grant is unusable ⇒ the user signs in again
  throw new OidcError('invalid_grant', `token endpoint: ${body.error ?? `HTTP ${res.status}`}`);
}

export function exchangeCode(cfg: BffConfig, code: string, verifier: string): Promise<TokenSet> {
  return tokenRequest(
    cfg,
    new URLSearchParams({ grant_type: 'authorization_code', code, redirect_uri: redirectUri(cfg), code_verifier: verifier }),
  );
}

// Single-flight per refresh token: parallel requests of one browser (chat + feedback) must not both redeem a
// rotating refresh token — the second would get invalid_grant and sign the user out. (Per process: with several
// BFF replicas, use sticky sessions or accept a rare forced re-login.)
const refreshing = new Map<string, Promise<TokenSet>>();

/**
 * `rejectedAccessToken`: the agent answered 401 to this access token. When the shared result of an earlier refresh IS
 * that token (an IdP that does not rotate refresh tokens hands out the same one), reusing it would retry with the
 * token just rejected and sign the user out — a new refresh is made instead (still one per rejected token, shared).
 */
export async function refreshTokens(cfg: BffConfig, refreshToken: string, rejectedAccessToken?: string): Promise<TokenSet> {
  let p = refreshing.get(refreshToken);
  if (p && rejectedAccessToken) {
    const done = await p.catch(() => undefined);
    if (done?.access_token === rejectedAccessToken && refreshing.get(refreshToken) === p) refreshing.delete(refreshToken);
    p = refreshing.get(refreshToken); // a parallel request may already have started the new refresh
  }
  if (!p) {
    p = tokenRequest(cfg, new URLSearchParams({ grant_type: 'refresh_token', refresh_token: refreshToken }));
    refreshing.set(refreshToken, p);
    const forget = () => setTimeout(() => refreshing.delete(refreshToken), 30_000); // late duplicates reuse it
    p.then(forget, () => refreshing.delete(refreshToken));
  }
  return p;
}

/**
 * Claims of an ID token received DIRECTLY from the token endpoint over TLS (OIDC Core §3.1.3.7: TLS server
 * validation may replace the signature check here). iss / aud / nonce / exp are still checked.
 * It is used only for the UI name and the sign-out hint — the agent authenticates the ACCESS token itself (JWKS).
 */
export function idTokenClaims(
  idToken: string,
  expect: { issuer: string; clientId: string; nonce: string },
): { sub: string; name?: string } {
  let c: Record<string, unknown>;
  try {
    c = JSON.parse(new TextDecoder().decode(b64urlDecode(idToken.split('.')[1] ?? ''))) as Record<string, unknown>;
  } catch {
    throw new OidcError('invalid', 'ID token is not a JWT');
  }
  const aud = Array.isArray(c.aud) ? c.aud : [c.aud];
  if (typeof c.iss !== 'string' || c.iss.replace(/\/+$/, '') !== expect.issuer) throw new OidcError('invalid', 'ID token iss');
  if (!aud.includes(expect.clientId)) throw new OidcError('invalid', 'ID token aud');
  if (c.nonce !== expect.nonce) throw new OidcError('invalid', 'ID token nonce');
  if (typeof c.exp !== 'number' || c.exp < Date.now() / 1000 - 60) throw new OidcError('invalid', 'ID token expired');
  if (typeof c.sub !== 'string' || !c.sub) throw new OidcError('invalid', 'ID token sub');
  const name = [c.name, c.preferred_username, c.email].find((v): v is string => typeof v === 'string' && !!v);
  return { sub: c.sub, name };
}

/** RFC 7009 revocation of the refresh token at sign-out — best effort (not every IdP offers it). */
export async function revoke(cfg: BffConfig, refreshToken: string): Promise<void> {
  try {
    const d = await discovery(cfg);
    if (!d.revocation_endpoint) return;
    const form = new URLSearchParams({ token: refreshToken, token_type_hint: 'refresh_token' });
    const headers = { 'Content-Type': 'application/x-www-form-urlencoded', ...clientAuth(cfg, d, form) };
    await fetch(d.revocation_endpoint, { method: 'POST', headers, body: form, signal: AbortSignal.timeout(TIMEOUT_MS) });
  } catch {
    // the local session is gone anyway
  }
}

/** RP-initiated logout URL (ends the IdP session, else the next "Sign in" logs straight back in); null if none. */
export async function endSessionUrl(cfg: BffConfig, idToken?: string): Promise<string | null> {
  const d = await discovery(cfg).catch(() => null);
  if (!d?.end_session_endpoint) return null;
  const u = new URL(d.end_session_endpoint);
  u.searchParams.set('client_id', cfg.oidc.clientId);
  u.searchParams.set('post_logout_redirect_uri', `${cfg.appOrigin}/`);
  if (idToken) u.searchParams.set('id_token_hint', idToken);
  return u.toString();
}
