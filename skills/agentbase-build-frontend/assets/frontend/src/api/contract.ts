// Payload / response types of POST /invocations — mirrors the contract in backend app/service.py
// (/agentbase-build → references/api-contract.md). Pure TypeScript (no React Native / Expo imports) so the chat
// state reducer that uses it can be unit-tested in plain Node.

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
  // 409 while an approval is pending carries that approval (`interrupt`) so the client can show it again
  | { event: 'error'; message: string; status?: number; interrupt?: unknown };

/** done / interrupt / error end a turn: nothing after them may change the result. */
export const isTerminal = (e: { event: string }) => e.event === 'done' || e.event === 'interrupt' || e.event === 'error';

export type Body =
  | { type: 'chat'; message: string }
  | { type: 'resume'; decisions: Decision[]; interrupt_id?: string | null };

export const chatBody = (message: string): Body => ({ type: 'chat', message });
// interrupt_id: backend rejects (409) if the interrupt was already handled — prevents a double-tap running the tool twice
export const resumeBody = (decisions: Decision[], interruptId?: string | null): Body => ({
  type: 'resume',
  decisions,
  interrupt_id: interruptId ?? null,
});

function isInterrupt(v: unknown): v is Interrupt {
  if (!v || typeof v !== 'object') return false;
  const calls = (v as { tool_calls?: unknown }).tool_calls;
  return (
    Array.isArray(calls) &&
    calls.length > 0 &&
    calls.every((c) => !!c && typeof c === 'object' && typeof (c as { id?: unknown }).id === 'string')
  );
}

/**
 * The pending approval carried by a 409: `{"interrupt": {...}}` in an SSE `error` event, or
 * `{"error", "error_type", "details": {"interrupt": {...}}}` in an HTTP error body. undefined when absent/garbled.
 */
export function interruptFrom(body: unknown): Interrupt | undefined {
  if (!body || typeof body !== 'object') return undefined;
  const b = body as { interrupt?: unknown; details?: { interrupt?: unknown } | null };
  const candidate = b.interrupt ?? (b.details && typeof b.details === 'object' ? b.details.interrupt : undefined);
  if (!isInterrupt(candidate)) return undefined;
  return {
    id: typeof candidate.id === 'string' ? candidate.id : null,
    type: 'tool_approval',
    message: typeof candidate.message === 'string' ? candidate.message : '',
    tool_calls: candidate.tool_calls.map((c) => ({
      id: c.id,
      name: typeof c.name === 'string' ? c.name : '?',
      args: c.args && typeof c.args === 'object' && !Array.isArray(c.args) ? c.args : {},
    })),
  };
}
