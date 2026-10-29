// Small response helpers of the BFF route handlers (server only).
import { NextResponse } from 'next/server';

import { type BffConfig, bffConfig, ConfigError } from './config';
import { errorBody } from './upstream';

const NO_STORE = { 'Cache-Control': 'no-store' };

export const json = (status: number, body: unknown) =>
  new NextResponse(typeof body === 'string' ? body : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...NO_STORE },
  });

/** The configuration, or a 500 response (the reason is logged, never sent to the browser). */
export function configOr500(): BffConfig | NextResponse {
  try {
    return bffConfig();
  } catch (e) {
    console.error(e instanceof ConfigError ? e.message : e);
    return json(500, errorBody('The web app is misconfigured — see the server log', 'config_error'));
  }
}

/** Codes the sign-in screen knows how to explain (never free text from the URL). */
export type AuthErrorCode = 'idp_error' | 'idp_unavailable' | 'state' | 'login_failed' | 'config';

export const redirectHome = (cfg: BffConfig, error?: AuthErrorCode) =>
  NextResponse.redirect(new URL(error ? `/?auth_error=${error}` : '/', cfg.appOrigin), { status: 303, headers: NO_STORE });
