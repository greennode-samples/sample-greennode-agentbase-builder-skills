// Client for the AgentBase Runtime — follows the payload contract in backend app/service.py exactly.
// expo/fetch supports reading response.body as a stream on iOS/Android (needed for SSE).
import { fetch as expoFetch } from 'expo/fetch';

import { env } from '../config/env';

export type PendingToolCall = { id: string; name: string; args: Record<string, unknown> };
export type Interrupt = {
  id: string | null;
  type: 'tool_approval';
  message: string;
  tool_calls: PendingToolCall[];
};
export type Decision = {
  tool_call_id: string;
  action: 'approve' | 'edit' | 'reject';
  args?: Record<string, unknown>;
  reason?: string;
};

export type ChatResult =
  | {
      status: 'success';
      response: string;
      tools_used: string[];
      session_id: string;
      trace_id: string | null;
      feedback_token: string | null; // HMAC(user, trace) — required when sending feedback
    }
  | {
      status: 'interrupted';
      interrupt: Interrupt;
      session_id: string;
      trace_id: string | null;
      feedback_token: string | null;
    };

export type StreamEvent =
  | { event: 'token'; data: string }
  | { event: 'tool_start'; name: string }
  | { event: 'tool_end'; name: string; status?: string }
  | { event: 'reset'; reason: string }
  | ({ event: 'interrupt' } & Extract<ChatResult, { status: 'interrupted' }>)
  | ({ event: 'done' } & Extract<ChatResult, { status: 'success' }>)
  | { event: 'error'; message: string; status?: number };

export class AgentError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

type Ctx = { sessionId: string; userId: string | null; accessToken: string | null };

function headers({ sessionId, userId, accessToken }: Ctx): Record<string, string> {
  const h: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-GreenNode-AgentBase-Session-Id': sessionId,
  };
  // jwt: backend takes the user from the token (sending it risks 403 if AUTH_USER_CLAIM isn't `sub`) ⇒ only send when none
  if (userId && env.authMode !== 'jwt') h['X-GreenNode-AgentBase-User-Id'] = userId;
  if (accessToken) h[env.authTokenHeader] = `Bearer ${accessToken}`;
  return h;
}

async function post(body: unknown, ctx: Ctx, accept = 'application/json', signal?: AbortSignal) {
  const res = await expoFetch(`${env.agentUrl}/invocations`, {
    method: 'POST',
    headers: { ...headers(ctx), Accept: accept },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      msg = ((await res.json()) as { error?: string }).error ?? msg;
    } catch {}
    throw new AgentError(res.status, msg);
  }
  return res;
}

type Body =
  | { type: 'chat'; message: string }
  | { type: 'resume'; decisions: Decision[]; interrupt_id?: string | null };

export const chatBody = (message: string): Body => ({ type: 'chat', message });
// interrupt_id: backend rejects (409) if the interrupt was already handled — prevents a double-tap running the tool twice
export const resumeBody = (decisions: Decision[], interruptId?: string | null): Body => ({
  type: 'resume',
  decisions,
  interrupt_id: interruptId ?? null,
});

// `signal` (AbortController): cancel the request, e.g. when the user starts a new conversation mid-turn.
export async function invoke(body: Body, ctx: Ctx, signal?: AbortSignal): Promise<ChatResult> {
  const res = await post({ ...body, stream: false }, ctx, 'application/json', signal);
  return (await res.json()) as ChatResult;
}

/**
 * Incremental SSE parser (WHATWG event-stream framing): LF, CRLF or CR line endings (a CRLF split across two
 * chunks included), an event ends at a blank line, several `data:` lines are joined with "\n", `:` comment /
 * keep-alive lines and other fields are ignored. `end()` flushes a last event that lacks the closing blank line
 * (some proxies strip it) — otherwise the final `done`/`interrupt` would be lost.
 */
export function createSseParser(onData: (data: string) => void) {
  let buffer = '';
  let data: string[] = [];
  const dispatch = () => {
    if (data.length) onData(data.join('\n'));
    data = [];
  };
  const line = (l: string) => {
    if (l === '') return dispatch();
    if (l.startsWith(':')) return;
    const colon = l.indexOf(':');
    const field = colon < 0 ? l : l.slice(0, colon);
    const value = colon < 0 ? '' : l.slice(colon + 1).replace(/^ /, '');
    if (field === 'data') data.push(value);
  };
  return {
    push(chunk: string) {
      buffer += chunk;
      const held = buffer.endsWith('\r'); // maybe the first half of a CRLF: decide when the next chunk arrives
      const lines = (held ? buffer.slice(0, -1) : buffer).split(/\r\n|\r|\n/);
      buffer = lines.pop()! + (held ? '\r' : '');
      lines.forEach(line);
    },
    end() {
      if (buffer) line(buffer.replace(/\r$/, ''));
      buffer = '';
      dispatch();
    },
  };
}

export async function invokeStream(
  body: Body,
  ctx: Ctx,
  onEvent: (e: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await post({ ...body, stream: true }, ctx, 'text/event-stream', signal);
  const reader = res.body?.getReader();
  if (!reader) throw new AgentError(0, 'Streaming not supported on this platform');
  const parser = createSseParser((data) => {
    let event: StreamEvent;
    try {
      event = JSON.parse(data) as StreamEvent;
    } catch {
      // Truncated/garbled frame: skip it (the caller notices a missing final event) instead of killing the stream
      if (__DEV__) console.warn('[agent] unparsable SSE frame skipped:', data.slice(0, 200));
      return;
    }
    onEvent(event);
  });
  const decoder = new TextDecoder();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    parser.push(decoder.decode(value, { stream: true }));
  }
  parser.push(decoder.decode()); // flush a trailing partial UTF-8 sequence
  parser.end();
}

export async function sendFeedback(
  traceId: string,
  feedbackToken: string, // from the same response — the backend answers 403 without it
  score: -1 | 0 | 1,
  ctx: Ctx,
  comment?: string,
) {
  await post({ type: 'feedback', trace_id: traceId, feedback_token: feedbackToken, score, comment }, ctx);
}
