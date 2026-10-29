// BFF session in a sealed, chunked, httpOnly cookie (server only). The browser never sees a token: it holds an
// encrypted blob it cannot read, and the BFF unseals it per request. No server-side store ⇒ any BFF replica works.
import type { NextRequest, NextResponse } from 'next/server';

import type { BffConfig } from './config';
import { cookieNames, cookieOptions, joinChunks, MAX_CHUNKS, splitChunks, staleChunks } from './cookies';
import { seal, unseal } from './crypto';

export type Session = {
  /** user id shown in the UI and used as the conversation owner (the AGENT derives its own from the token) */
  sub: string;
  name?: string;
  csrf: string;
  accessToken?: string;
  /** epoch seconds */
  accessTokenExp?: number;
  refreshToken?: string;
  /** only for `id_token_hint` at sign-out; dropped when the cookie would be too large */
  idToken?: string;
  /** epoch seconds of the sign-in: the session ends BFF_SESSION_MAX_AGE_S after it, however often tokens refresh */
  authTime?: number;
};

/** OIDC transaction between /api/auth/login and /api/auth/callback (10 minutes). */
export type OidcTx = { state: string; nonce: string; verifier: string };
const OIDC_TX_MAX_AGE_S = 600;

const nowS = () => Math.floor(Date.now() / 1000);

/** Seconds the session has left: BFF_SESSION_MAX_AGE_S counted from the sign-in (absolute, never extended). */
export const sessionSecondsLeft = (s: Pick<Session, 'authTime'>, cfg: Pick<BffConfig, 'sessionMaxAgeS'>, now = nowS()) =>
  (s.authTime ?? now) + cfg.sessionMaxAgeS - now;

export async function readSession(req: NextRequest, cfg: BffConfig): Promise<Session | null> {
  const { session } = cookieNames(cfg.secure);
  const sealed = joinChunks(session, (n) => req.cookies.get(n)?.value);
  const s = sealed ? await unseal<Session>(sealed, cfg.sessionSecret, session) : null;
  // also ends sessions sealed before BFF_SESSION_MAX_AGE_S was lowered
  return s && s.authTime !== undefined && sessionSecondsLeft(s, cfg) <= 0 ? null : s;
}

/** Seals the session for the time it has LEFT: rewriting it (token refresh) must not extend it — a stolen cookie
 * otherwise stayed valid as long as the IdP kept refreshing. */
export async function writeSession(res: NextResponse, req: NextRequest, cfg: BffConfig, s: Session): Promise<void> {
  const { session } = cookieNames(cfg.secure);
  s = { ...s, authTime: s.authTime ?? nowS() };
  const left = sessionSecondsLeft(s, cfg);
  if (left <= 0) {
    clearSession(res, req, cfg);
    return;
  }
  let sealed = await seal(s, cfg.sessionSecret, session, left);
  if (splitChunks(session, sealed).length > MAX_CHUNKS && s.idToken) {
    // Too large: sign-out then works without id_token_hint (the IdP may ask to confirm)
    sealed = await seal({ ...s, idToken: undefined }, cfg.sessionSecret, session, left);
  }
  const chunks = splitChunks(session, sealed);
  for (const c of chunks) res.cookies.set(c.name, c.value, cookieOptions(cfg.secure, left));
  const existing = req.cookies.getAll().map((c) => c.name);
  for (const n of staleChunks(session, existing, chunks.length)) res.cookies.set(n, '', cookieOptions(cfg.secure, 0));
}

export function clearSession(res: NextResponse, req: NextRequest, cfg: BffConfig): void {
  const { session } = cookieNames(cfg.secure);
  for (const n of staleChunks(session, req.cookies.getAll().map((c) => c.name), 0)) {
    res.cookies.set(n, '', cookieOptions(cfg.secure, 0));
  }
}

export async function writeOidcTx(res: NextResponse, cfg: BffConfig, tx: OidcTx): Promise<void> {
  const { oidc } = cookieNames(cfg.secure);
  res.cookies.set(oidc, await seal(tx, cfg.sessionSecret, oidc, OIDC_TX_MAX_AGE_S), cookieOptions(cfg.secure, OIDC_TX_MAX_AGE_S));
}

export async function readOidcTx(req: NextRequest, cfg: BffConfig): Promise<OidcTx | null> {
  const { oidc } = cookieNames(cfg.secure);
  const v = req.cookies.get(oidc)?.value;
  return v ? unseal<OidcTx>(v, cfg.sessionSecret, oidc) : null;
}

export function clearOidcTx(res: NextResponse, cfg: BffConfig): void {
  res.cookies.set(cookieNames(cfg.secure).oidc, '', cookieOptions(cfg.secure, 0));
}
