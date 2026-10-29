// GET /api/auth/login — starts OIDC Authorization Code + PKCE: state, nonce and the PKCE verifier go into a sealed
// short-lived cookie, the browser is redirected to the IdP.
import { type NextRequest, NextResponse } from 'next/server';

import { pkceChallenge, randomToken } from '../../../../server/crypto';
import { configOr500, redirectHome } from '../../../../server/http';
import { authorizationUrl, discovery } from '../../../../server/oidc';
import { writeOidcTx } from '../../../../server/session';

export const dynamic = 'force-dynamic';

export async function GET(_req: NextRequest) {
  const cfg = configOr500();
  if (cfg instanceof NextResponse) return cfg;
  if (cfg.authMode === 'none') return redirectHome(cfg);
  try {
    const tx = { state: randomToken(), nonce: randomToken(), verifier: randomToken() };
    const url = authorizationUrl(cfg, await discovery(cfg), { ...tx, challenge: await pkceChallenge(tx.verifier) });
    const res = NextResponse.redirect(url, { status: 303, headers: { 'Cache-Control': 'no-store' } });
    await writeOidcTx(res, cfg, tx);
    return res;
  } catch (e) {
    console.error('[auth] login:', e instanceof Error ? e.message : e);
    return redirectHome(cfg, 'idp_unavailable');
  }
}
