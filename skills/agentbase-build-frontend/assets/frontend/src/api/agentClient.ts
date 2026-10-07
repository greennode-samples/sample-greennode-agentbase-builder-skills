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

async function post(body: unknown, ctx: Ctx, accept = 'application/json') {
  const res = await expoFetch(`${env.agentUrl}/invocations`, {
    method: 'POST',
    headers: { ...headers(ctx), Accept: accept },
    body: JSON.stringify(body),
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

export async function invoke(body: Body, ctx: Ctx): Promise<ChatResult> {
  const res = await post({ ...body, stream: false }, ctx);
  return (await res.json()) as ChatResult;
}

export async function invokeStream(
  body: Body,
  ctx: Ctx,
  onEvent: (e: StreamEvent) => void,
): Promise<void> {
  const res = await post({ ...body, stream: true }, ctx, 'text/event-stream');
  const reader = res.body?.getReader();
  if (!reader) throw new AgentError(0, 'Streaming not supported on this platform');
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      for (const line of frame.split('\n')) {
        if (line.startsWith('data:')) onEvent(JSON.parse(line.slice(5).trim()) as StreamEvent);
      }
    }
  }
}

export async function sendFeedback(
  traceId: string,
  feedbackToken: string,
  score: -1 | 0 | 1,
  ctx: Ctx,
  comment?: string,
) {
  await post({ type: 'feedback', trace_id: traceId, feedback_token: feedbackToken, score, comment }, ctx);
}
