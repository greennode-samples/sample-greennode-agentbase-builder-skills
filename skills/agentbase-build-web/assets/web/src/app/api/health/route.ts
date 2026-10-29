// GET /api/health — is this process up. Liveness for a container platform / Docker HEALTHCHECK / a post-deploy smoke
// test. It reads no configuration and calls no IdP or agent: an outage elsewhere must not restart the web app.
import { NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';

export function GET() {
  return NextResponse.json({ status: 'ok' }, { headers: { 'Cache-Control': 'no-store' } });
}
