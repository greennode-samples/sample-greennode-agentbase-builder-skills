// Next.js Proxy (formerly Middleware): a fresh CSP nonce per page request. API routes are excluded (they return JSON /
// SSE and get the static headers of next.config.ts).
import { type NextRequest, NextResponse } from 'next/server';

import { contentSecurityPolicy } from './lib/csp';

export function proxy(request: NextRequest) {
  const nonce = btoa(crypto.randomUUID());
  const csp = contentSecurityPolicy(nonce, process.env.NODE_ENV === 'development');
  const headers = new Headers(request.headers);
  headers.set('x-nonce', nonce);
  headers.set('Content-Security-Policy', csp); // Next.js reads the nonce from here while rendering
  const response = NextResponse.next({ request: { headers } });
  response.headers.set('Content-Security-Policy', csp);
  return response;
}

export const config = {
  matcher: [
    {
      source: '/((?!api/|_next/static|_next/image|favicon.ico).*)', // `api/`: a page like /apiary still gets its CSP
      missing: [
        { type: 'header', key: 'next-router-prefetch' },
        { type: 'header', key: 'purpose', value: 'prefetch' },
      ],
    },
  ],
};
