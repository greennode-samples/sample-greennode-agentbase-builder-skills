// POST /api/auth/logout (CSRF-protected) — clears the session cookie first, revokes the refresh token (best effort,
// RFC 7009) and returns the IdP end-session URL the browser navigates to (else the next sign-in is silent).
import { type NextRequest, NextResponse } from 'next/server';

import { CSRF_HEADER, csrfViolation } from '../../../../server/csrf';
import { configOr500, json } from '../../../../server/http';
import { endSessionUrl, revoke } from '../../../../server/oidc';
import { clearSession, readSession } from '../../../../server/session';
import { errorBody } from '../../../../server/upstream';

export const dynamic = 'force-dynamic';

export async function POST(req: NextRequest) {
  const cfg = configOr500();
  if (cfg instanceof NextResponse) return cfg;
  const s = await readSession(req, cfg);
  const h = req.headers;
  const violation = csrfViolation(
    { method: req.method, origin: h.get('origin'), secFetchSite: h.get('sec-fetch-site'), token: h.get(CSRF_HEADER) },
    cfg.appOrigin,
    s?.csrf,
  );
  if (s && violation) {
    console.warn(`[auth] logout refused: ${violation}`);
    return json(403, errorBody('Forbidden', 'csrf'));
  }
  let redirect = '/';
  if (s && cfg.authMode === 'jwt') {
    if (s.refreshToken) await revoke(cfg, s.refreshToken);
    redirect = (await endSessionUrl(cfg, s.idToken)) ?? '/';
  }
  const res = json(200, { redirect });
  clearSession(res, req, cfg);
  return res;
}
