// POST /api/invocations — the BFF proxy to the AgentBase Runtime. Same contract as the agent's POST /invocations
// (payloads, status codes, error bodies incl. 409 + details.interrupt, SSE) so the chat code is the one of the Expo
// app. Adds what the browser must not hold: the user's access token (refreshed here) and, for an IAM-protected
// Runtime, the IAM token.
import { type NextRequest, NextResponse } from 'next/server';

import { CSRF_HEADER, csrfViolation } from '../../../server/csrf';
import { configOr500, json } from '../../../server/http';
import { iamToken } from '../../../server/iam';
import { OidcError } from '../../../server/oidc';
import { clearSession, readSession, writeSession } from '../../../server/session';
import { freshSession } from '../../../server/tokens';
import {
  downstreamHeaders,
  errorBody,
  parseBody,
  SESSION_HEADER,
  SESSION_ID_RE,
  upstreamHeaders,
} from '../../../server/upstream';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

export async function POST(req: NextRequest) {
  const cfg = configOr500();
  if (cfg instanceof NextResponse) return cfg;
  const h = req.headers;
  let session = await readSession(req, cfg);
  if (!session) return json(401, errorBody('Not signed in', 'unauthenticated'));
  const violation = csrfViolation(
    { method: req.method, origin: h.get('origin'), secFetchSite: h.get('sec-fetch-site'), token: h.get(CSRF_HEADER) },
    cfg.appOrigin,
    session.csrf,
  );
  if (violation) {
    console.warn(`[bff] request refused: ${violation}`);
    return json(403, errorBody('Forbidden', 'csrf'));
  }
  const sessionId = h.get(SESSION_HEADER) ?? '';
  if (!SESSION_ID_RE.test(sessionId)) return json(400, errorBody(`Invalid ${SESSION_HEADER}`, 'bad_request'));
  const parsed = parseBody(await req.text());
  if ('error' in parsed) return json(400, errorBody(parsed.error, 'bad_request'));

  let changed = false;
  const call = async (force: boolean) => {
    if (cfg.authMode === 'jwt') {
      const r = await freshSession(cfg, session!, force);
      session = r.session;
      changed ||= r.changed;
    }
    const iam = cfg.runtimeAuth === 'iam' ? await iamToken(cfg, force) : undefined;
    return fetch(`${cfg.agentUrl}/invocations`, {
      method: 'POST',
      headers: upstreamHeaders(cfg, {
        sessionId,
        accept: h.get('accept') ?? 'application/json',
        userId: session!.sub,
        userToken: session!.accessToken,
        iamToken: iam,
      }),
      body: JSON.stringify(parsed.body),
      signal: req.signal, // the browser pressed Stop / closed the tab ⇒ the agent call is aborted too
      cache: 'no-store',
    });
  };

  let upstream: Response;
  try {
    upstream = await call(false);
    if (upstream.status === 401 && cfg.authMode === 'jwt') {
      // token revoked / rotated early: refresh once and retry (the turn did not run — 401 is answered before it)
      await upstream.body?.cancel();
      upstream = await call(true);
    }
  } catch (e) {
    if (e instanceof OidcError && e.kind === 'invalid_grant') {
      const res = json(401, errorBody('Your session has expired — sign in again', 'unauthenticated'));
      clearSession(res, req, cfg);
      return res;
    }
    if (req.signal.aborted) return new NextResponse(null, { status: 499 });
    console.error('[bff] upstream:', e instanceof Error ? e.message : e);
    // 503 = "before the turn ran, retry with backoff" in the contract: true for an unreachable IdP / IAM / agent
    return json(503, errorBody('The agent or the sign-in provider is unreachable — try again', 'unavailable'));
  }

  const res = new NextResponse(upstream.body, {
    status: upstream.status,
    headers: downstreamHeaders(upstream.headers.get('content-type')),
  });
  if (upstream.status === 401) clearSession(res, req, cfg); // still 401 with a fresh token: sign in again
  else if (changed) await writeSession(res, req, cfg, session);
  return res;
}
