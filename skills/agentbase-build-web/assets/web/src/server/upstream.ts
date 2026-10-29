// What the BFF forwards to the Runtime (server only). Pure — no Next.js imports — unit-tested in tests/upstream.test.ts.
// The /invocations contract stays UNCHANGED (/agentbase-build → references/api-contract.md): same payloads, same
// status codes and error bodies (409 + details.interrupt included), SSE passed through verbatim and unbuffered.
import type { BffConfig } from './config';

export const SESSION_HEADER = 'X-GreenNode-AgentBase-Session-Id';
export const SESSION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9-]{0,127}$/;
// Backend limits: message MAX_MESSAGE_CHARS, edit args ≤ 16 000 chars of JSON — 64 KiB covers them with margin
export const MAX_BODY_BYTES = 64 * 1024;
const TYPES = new Set(['chat', 'resume', 'feedback']);

/** The request body to forward (re-serialized JSON object of a known type), or why it is refused (400). */
export function parseBody(text: string): { body: Record<string, unknown> } | { error: string } {
  if (new TextEncoder().encode(text).length > MAX_BODY_BYTES) return { error: 'Request body too large' };
  let v: unknown;
  try {
    v = JSON.parse(text);
  } catch {
    return { error: 'Request body is not JSON' };
  }
  if (!v || typeof v !== 'object' || Array.isArray(v)) return { error: 'Request body must be a JSON object' };
  const type = (v as { type?: unknown }).type;
  if (typeof type !== 'string' || !TYPES.has(type)) return { error: 'Unknown request type' };
  return { body: v as Record<string, unknown> };
}

/**
 * Headers for `<agentUrl>/invocations`. Only these — browser headers (cookies!) are never forwarded.
 *  - authMode none (local): X-GreenNode-AgentBase-User-Id = the local user, no token
 *  - runtimeAuth jwt: Authorization: Bearer <user access token> (backend AUTH_TOKEN_HEADER=Authorization)
 *  - runtimeAuth iam: Authorization: Bearer <IAM token> + X-GreenNode-AgentBase-Custom-User-Token: Bearer <user
 *    access token> (backend AUTH_TOKEN_HEADER=X-GreenNode-AgentBase-Custom-User-Token)
 * In jwt mode no User-Id header is sent: the agent takes the user from the token's `sub`.
 */
export function upstreamHeaders(
  cfg: Pick<BffConfig, 'authMode' | 'runtimeAuth'>,
  p: { sessionId: string; accept: string; userId: string; userToken?: string; iamToken?: string },
): Record<string, string> {
  const h: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: p.accept === 'text/event-stream' ? 'text/event-stream' : 'application/json',
    [SESSION_HEADER]: p.sessionId,
  };
  if (cfg.authMode === 'none') h['X-GreenNode-AgentBase-User-Id'] = p.userId;
  if (cfg.runtimeAuth === 'iam') {
    if (p.iamToken) h.Authorization = `Bearer ${p.iamToken}`;
    if (p.userToken) h['X-GreenNode-AgentBase-Custom-User-Token'] = `Bearer ${p.userToken}`;
  } else if (p.userToken) {
    h.Authorization = `Bearer ${p.userToken}`;
  }
  return h;
}

/** Response headers to the browser: content type of the agent, never cached, never compressed or buffered. */
export function downstreamHeaders(upstreamContentType: string | null): Record<string, string> {
  return {
    'Content-Type': upstreamContentType ?? 'application/json',
    // no-transform: Next.js / proxies must not gzip the SSE stream (gzip buffers it — tokens arrive in one block)
    'Cache-Control': 'no-store, no-transform',
    'X-Accel-Buffering': 'no',
  };
}

/** Error body in the backend's SDK format, so the client handles BFF errors like agent errors. */
export const errorBody = (error: string, errorType: string) => JSON.stringify({ error, error_type: errorType, details: null });
