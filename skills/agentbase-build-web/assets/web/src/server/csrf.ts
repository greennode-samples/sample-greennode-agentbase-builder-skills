// CSRF protection of the BFF's state-changing routes (server only). Pure — no Next.js imports.
// Two independent checks, both required on POST:
//   1. the request comes from our own origin (Origin header, else Sec-Fetch-Site: same-origin);
//   2. it carries the per-session CSRF token (header X-CSRF-Token) — a cross-site page can trigger a request with
//      the session cookie (SameSite=Lax blocks most, not all) but can neither read nor guess the token.
import { timingSafeEqual } from './crypto';

export const CSRF_HEADER = 'X-CSRF-Token';

export type CsrfInput = {
  method: string;
  origin: string | null;
  secFetchSite: string | null;
  token: string | null;
};

/** null when the request may proceed, else the reason it is rejected (logged, never sent to the browser). */
export function csrfViolation(req: CsrfInput, appOrigin: string, sessionToken: string | null | undefined): string | null {
  if (req.method === 'GET' || req.method === 'HEAD' || req.method === 'OPTIONS') return null;
  if (req.origin !== null) {
    if (req.origin !== appOrigin) return `cross-origin request from ${req.origin.slice(0, 100)}`;
  } else if (req.secFetchSite !== 'same-origin') {
    return 'no Origin and no Sec-Fetch-Site: same-origin';
  }
  if (!sessionToken) return 'no session';
  if (!req.token || !timingSafeEqual(req.token, sessionToken)) return 'missing or wrong CSRF token';
  return null;
}
