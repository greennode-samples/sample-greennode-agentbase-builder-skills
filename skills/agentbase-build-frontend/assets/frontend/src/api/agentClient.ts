// Client for the AgentBase Runtime — follows the payload contract in backend app/service.py exactly.
// expo/fetch supports reading response.body as a stream on iOS/Android (needed for SSE).
import { fetch as expoFetch } from 'expo/fetch';

import { env } from '../config/env';
import { type Body, type ChatResult, type Interrupt, interruptFrom, isTerminal, type StreamEvent } from './contract';
import { createSseParser } from './sse';

export * from './contract';
export { createSseParser } from './sse';

export class AgentError extends Error {
  /** 409 "an approval is pending": that approval (show it again instead of a dead end) */
  readonly interrupt?: Interrupt;
  /** no data from the agent for `EXPO_PUBLIC_IDLE_TIMEOUT_S` */
  readonly timeout: boolean;
  constructor(
    public status: number,
    message: string,
    extra: { interrupt?: Interrupt; timeout?: boolean } = {},
  ) {
    super(message);
    this.interrupt = extra.interrupt;
    this.timeout = !!extra.timeout;
  }
}

type Ctx = { sessionId: string; userId: string | null; accessToken: string | null };
export type RequestOptions = {
  /** cancel the request (Stop, New conversation) */
  signal?: AbortSignal;
  /** abort when NO data arrives for this long (re-armed on every chunk); 0 = no limit */
  idleTimeoutMs?: number;
};

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
    let interrupt: Interrupt | undefined;
    try {
      const err = (await res.json()) as { error?: unknown };
      if (typeof err.error === 'string' && err.error) msg = err.error;
      interrupt = interruptFrom(err); // 409 while an approval is pending: details.interrupt
    } catch {}
    throw new AgentError(res.status, msg, { interrupt });
  }
  return res;
}

/** AbortController linked to the caller's signal + an inactivity timer (`kick()` re-arms it on every chunk). */
function watchdog(opts: RequestOptions) {
  const ctrl = new AbortController();
  const idleMs = opts.idleTimeoutMs ?? 0;
  let timedOut = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const onAbort = () => ctrl.abort();
  if (opts.signal?.aborted) ctrl.abort();
  else opts.signal?.addEventListener('abort', onAbort);
  const kick = () => {
    if (!idleMs) return;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timedOut = true;
      ctrl.abort();
    }, idleMs);
  };
  kick();
  return {
    signal: ctrl.signal,
    kick,
    /** the abort caused by the idle timer ⇒ a readable AgentError; anything else unchanged */
    wrap: (err: unknown) =>
      timedOut
        ? new AgentError(0, `No response from the agent for ${Math.round(idleMs / 1000)}s — stopped waiting.`, {
            timeout: true,
          })
        : err,
    dispose() {
      if (timer) clearTimeout(timer);
      opts.signal?.removeEventListener('abort', onAbort);
    },
  };
}

export async function invoke(body: Body, ctx: Ctx, opts: RequestOptions = {}): Promise<ChatResult> {
  const wd = watchdog(opts);
  try {
    const res = await post({ ...body, stream: false }, ctx, 'application/json', wd.signal);
    return (await res.json()) as ChatResult;
  } catch (err) {
    throw wd.wrap(err);
  } finally {
    wd.dispose();
  }
}

/**
 * Streams one turn. Reading STOPS at the first final event (`done` / `interrupt` / `error`): anything after it —
 * including a transport error while the connection closes — must never overwrite that result. If `onEvent` throws,
 * the stream is cancelled too and the error propagates.
 */
export async function invokeStream(
  body: Body,
  ctx: Ctx,
  onEvent: (e: StreamEvent) => void,
  opts: RequestOptions = {},
): Promise<void> {
  const wd = watchdog(opts);
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  let eof = false;
  try {
    const res = await post({ ...body, stream: true }, ctx, 'text/event-stream', wd.signal);
    reader = res.body?.getReader();
    if (!reader) throw new AgentError(0, 'Streaming not supported on this platform');
    let terminal = false;
    const parser = createSseParser((data) => {
      if (terminal) return; // nothing after the final event
      let event: StreamEvent;
      try {
        event = JSON.parse(data) as StreamEvent;
      } catch {
        event = null as unknown as StreamEvent;
      }
      if (!event || typeof event !== 'object') {
        // Truncated/garbled frame: skip it (the caller notices a missing final event) instead of killing the stream
        if (__DEV__) console.warn('[agent] unparsable SSE frame skipped:', data.slice(0, 200));
        return;
      }
      if (isTerminal(event)) terminal = true;
      onEvent(event);
    });
    const decoder = new TextDecoder();
    while (!terminal) {
      const { value, done } = await reader.read();
      wd.kick();
      if (done) {
        eof = true;
        parser.push(decoder.decode()); // flush a trailing partial UTF-8 sequence
        parser.end();
        break;
      }
      parser.push(decoder.decode(value, { stream: true }));
    }
  } catch (err) {
    throw wd.wrap(err);
  } finally {
    wd.dispose();
    if (reader && !eof) {
      // final event received (or error): stop reading and release the connection
      try {
        void reader.cancel().catch(() => undefined);
      } catch {}
    }
  }
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
