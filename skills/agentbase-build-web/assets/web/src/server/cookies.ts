// Cookie names, attributes and chunking of the BFF session cookie (server only). Pure — no Next.js imports.
// A sealed session (access + refresh + ID token) is often larger than the ~4 KB a single cookie may hold, so it is
// split into `<name>.0`, `<name>.1`, … and joined again on read.

export const CHUNK_SIZE = 3800;
export const MAX_CHUNKS = 6; // ~22 KB: above that, request headers get too large for many proxies

/**
 * `__Host-` prefix on https: the browser then only accepts the cookie when it is Secure, Path=/ and has no Domain —
 * a sibling subdomain cannot plant or overwrite it. Plain names on http://localhost (development only).
 */
export function cookieNames(secure: boolean) {
  const p = secure ? '__Host-' : '';
  return { session: `${p}ab_session`, oidc: `${p}ab_oidc` };
}

/**
 * httpOnly (no JavaScript access — XSS cannot read tokens), Secure on https, SameSite=Lax: sent on the top-level
 * redirect back from the IdP (Strict would drop it there), never on cross-site POST / fetch. CSRF is checked too.
 */
export const cookieOptions = (secure: boolean, maxAge: number) =>
  ({ httpOnly: true, secure, sameSite: 'lax', path: '/', maxAge }) as const;

export function splitChunks(base: string, value: string): { name: string; value: string }[] {
  const out: { name: string; value: string }[] = [];
  for (let i = 0; i * CHUNK_SIZE < value.length || i === 0; i++) {
    out.push({ name: `${base}.${i}`, value: value.slice(i * CHUNK_SIZE, (i + 1) * CHUNK_SIZE) });
  }
  return out;
}

/** Joins `<base>.0`, `<base>.1`, … (contiguous from 0); null when there is no chunk 0. */
export function joinChunks(base: string, get: (name: string) => string | undefined): string | null {
  let out = '';
  for (let i = 0; i < MAX_CHUNKS; i++) {
    const v = get(`${base}.${i}`);
    if (v === undefined) break;
    out += v;
  }
  return out || null;
}

/** Chunk cookies of `base` the browser still has beyond the `keep` chunks just written — they must be deleted. */
export const staleChunks = (base: string, existing: string[], keep: number) =>
  existing.filter((n) => n.startsWith(`${base}.`) && Number(n.slice(base.length + 1)) >= keep);
