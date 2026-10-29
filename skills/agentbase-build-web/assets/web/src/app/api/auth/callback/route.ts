// GET /api/auth/callback — the IdP redirects here (register `<BFF_APP_ORIGIN>/api/auth/callback` as redirect URI).
// Checks state, redeems the code with the PKCE verifier (server-to-server), stores the tokens in the sealed session.
import { type NextRequest, NextResponse } from 'next/server';

import { randomToken, timingSafeEqual } from '../../../../server/crypto';
import { configOr500, redirectHome } from '../../../../server/http';
import { exchangeCode, idTokenClaims, OidcError } from '../../../../server/oidc';
import { clearOidcTx, readOidcTx, writeSession } from '../../../../server/session';
import { withTokens } from '../../../../server/tokens';

export const dynamic = 'force-dynamic';

export async function GET(req: NextRequest) {
  const cfg = configOr500();
  if (cfg instanceof NextResponse) return cfg;
  const q = req.nextUrl.searchParams;
  const tx = await readOidcTx(req, cfg);
  const fail = (code: Parameters<typeof redirectHome>[1], why: string) => {
    console.warn(`[auth] callback: ${why}`); // the IdP's error code only — never tokens
    const res = redirectHome(cfg, code);
    clearOidcTx(res, cfg);
    return res;
  };
  if (q.get('error')) return fail('idp_error', `IdP error ${q.get('error')?.slice(0, 100)}`);
  const code = q.get('code');
  const state = q.get('state');
  if (!tx || !code || !state || !timingSafeEqual(state, tx.state)) return fail('state', 'missing/mismatched state');
  try {
    const tokens = await exchangeCode(cfg, code, tx.verifier);
    if (!tokens.id_token) return fail('login_failed', 'no id_token (is "openid" in BFF_OIDC_SCOPES?)');
    const claims = idTokenClaims(tokens.id_token, { issuer: cfg.oidc.issuer, clientId: cfg.oidc.clientId, nonce: tx.nonce });
    const session = withTokens({ sub: claims.sub, name: claims.name, csrf: randomToken() }, tokens);
    const res = redirectHome(cfg);
    clearOidcTx(res, cfg);
    await writeSession(res, req, cfg, session);
    return res;
  } catch (e) {
    const unavailable = e instanceof OidcError && e.kind === 'unavailable';
    return fail(unavailable ? 'idp_unavailable' : 'login_failed', e instanceof Error ? e.message : String(e));
  }
}
