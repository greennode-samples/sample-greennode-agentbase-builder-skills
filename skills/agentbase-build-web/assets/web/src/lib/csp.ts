// Content-Security-Policy of the pages — pure, unit-tested in tests/upstream.test.ts. Used by src/proxy.ts with a fresh
// nonce per request (Next.js applies it to its own scripts and styles).
//  - script-src: only scripts carrying this request's nonce (+ what they load: 'strict-dynamic') — an injected
//    <script> or inline handler never runs; 'unsafe-eval' in development only (React's dev tooling)
//  - connect-src 'self': the browser talks to this app's /api routes only (the BFF) — not to the agent, not to the IdP
//  - frame-ancestors 'none' (no clickjacking), object-src / base-uri 'none', form-action 'self'
export function contentSecurityPolicy(nonce: string, dev: boolean): string {
  return [
    `default-src 'self'`,
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? ` 'unsafe-eval'` : ''}`,
    // dev: the error overlay injects styles without a nonce
    `style-src 'self' ${dev ? `'unsafe-inline'` : `'nonce-${nonce}'`}`,
    `img-src 'self' data: blob:`,
    `font-src 'self'`,
    `connect-src 'self'${dev ? ' ws: wss:' : ''}`,
    `object-src 'none'`,
    `base-uri 'none'`,
    `form-action 'self'`,
    `frame-ancestors 'none'`,
    ...(dev ? [] : ['upgrade-insecure-requests']),
  ].join('; ');
}
