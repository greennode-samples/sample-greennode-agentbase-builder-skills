// GET /api/auth/session — who is signed in + the CSRF token for POSTs. NEVER returns a token: they stay in the
// sealed httpOnly cookie. Readable by our own pages only (no CORS headers ⇒ other origins cannot read it).
import { type NextRequest, NextResponse } from 'next/server';

import { randomToken } from '../../../../server/crypto';
import { configOr500, json } from '../../../../server/http';
import { readSession, type Session, writeSession } from '../../../../server/session';

export const dynamic = 'force-dynamic';

export async function GET(req: NextRequest) {
  const cfg = configOr500();
  if (cfg instanceof NextResponse) return cfg;
  let s = await readSession(req, cfg);
  let created = false;
  if (!s && cfg.authMode === 'none') {
    // local development against a backend with AUTH_MODE=none: one fixed local user (refused in production)
    s = { sub: 'local-user', name: 'Local user', csrf: randomToken() } satisfies Session;
    created = true;
  }
  if (!s) return json(200, { authenticated: false, mode: cfg.authMode });
  const res = json(200, { authenticated: true, mode: cfg.authMode, user: { id: s.sub, name: s.name ?? null }, csrfToken: s.csrf });
  if (created) await writeSession(res, req, cfg, s);
  return res;
}
