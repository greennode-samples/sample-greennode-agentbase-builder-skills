import { describe, expect, it } from 'vitest';

import { contentSecurityPolicy } from '../src/lib/csp';
import { downstreamHeaders, parseBody, SESSION_ID_RE, upstreamHeaders } from '../src/server/upstream';

const p = { sessionId: 's-1', accept: 'text/event-stream', userId: 'u1', userToken: 'UT', iamToken: 'IAM' };

describe('BFF → Runtime headers', () => {
  it('jwt runtime: user token in Authorization, no User-Id header', () => {
    const h = upstreamHeaders({ authMode: 'jwt', runtimeAuth: 'jwt' }, p);
    expect(h).toMatchObject({ Authorization: 'Bearer UT', Accept: 'text/event-stream', 'X-GreenNode-AgentBase-Session-Id': 's-1' });
    expect(h['X-GreenNode-AgentBase-User-Id']).toBeUndefined();
  });
  it('iam runtime: IAM token in Authorization, user token in the custom header', () => {
    const h = upstreamHeaders({ authMode: 'jwt', runtimeAuth: 'iam' }, p);
    expect(h.Authorization).toBe('Bearer IAM');
    expect(h['X-GreenNode-AgentBase-Custom-User-Token']).toBe('Bearer UT');
  });
  it('none (local): User-Id header, no token', () => {
    const h = upstreamHeaders({ authMode: 'none', runtimeAuth: 'jwt' }, { ...p, userToken: undefined });
    expect(h['X-GreenNode-AgentBase-User-Id']).toBe('u1');
    expect(h.Authorization).toBeUndefined();
  });
  it('only JSON or SSE is requested; responses are never cached / compressed', () => {
    expect(upstreamHeaders({ authMode: 'jwt', runtimeAuth: 'jwt' }, { ...p, accept: 'text/html' }).Accept).toBe('application/json');
    expect(downstreamHeaders('text/event-stream')).toMatchObject({ 'Cache-Control': 'no-store, no-transform', 'X-Accel-Buffering': 'no' });
  });
});

describe('request validation', () => {
  it('accepts the contract payload types only', () => {
    expect(parseBody('{"type":"chat","message":"hi"}')).toEqual({ body: { type: 'chat', message: 'hi' } });
    expect(parseBody('{"type":"admin"}')).toEqual({ error: 'Unknown request type' });
    expect(parseBody('[1]')).toHaveProperty('error');
    expect(parseBody('nope')).toHaveProperty('error');
    expect(parseBody(JSON.stringify({ type: 'chat', message: 'x'.repeat(70_000) }))).toEqual({ error: 'Request body too large' });
  });
  it('session id format (blocks path traversal)', () => {
    expect(SESSION_ID_RE.test(crypto.randomUUID())).toBe(true);
    expect(SESSION_ID_RE.test('../x')).toBe(false);
  });
});

describe('CSP', () => {
  it('production: nonce scripts only, no unsafe-*, same-origin connections, no framing', () => {
    const csp = contentSecurityPolicy('N0NCE', false);
    expect(csp).toContain(`script-src 'self' 'nonce-N0NCE' 'strict-dynamic'`);
    expect(csp).not.toContain('unsafe');
    expect(csp).toContain(`connect-src 'self'`);
    expect(csp).toContain(`frame-ancestors 'none'`);
    expect(csp).toContain(`object-src 'none'`);
  });
  it('development adds only what the dev server needs', () => {
    expect(contentSecurityPolicy('N', true)).toContain(`'unsafe-eval'`);
  });
});
