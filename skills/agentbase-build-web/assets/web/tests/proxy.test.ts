import { describe, expect, it } from 'vitest';

import { config } from '../src/proxy';

describe('CSP proxy matcher', () => {
  const re = new RegExp(`^${config.matcher[0].source}$`);

  it('covers pages — also ones whose path merely starts with "api"', () => {
    for (const path of ['/', '/chat', '/apiary', '/api-docs', '/apis/x']) expect(re.test(path), path).toBe(true);
  });

  it('skips the JSON / SSE routes and static assets', () => {
    for (const path of ['/api/invocations', '/api/auth/session', '/_next/static/a.js', '/_next/image', '/favicon.ico']) {
      expect(re.test(path), path).toBe(false);
    }
  });
});

describe('GET /api/health', () => {
  it('answers 200 without any configuration and is never cached', async () => {
    const { GET } = await import('../src/app/api/health/route');
    const res = GET();
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ status: 'ok' });
    expect(res.headers.get('cache-control')).toBe('no-store');
  });
});
