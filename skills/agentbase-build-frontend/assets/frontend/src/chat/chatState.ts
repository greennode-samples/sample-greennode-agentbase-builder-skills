// Conversation state as a PURE reducer (no React / React Native imports) — every rule about turns, interrupts and
// errors lives here so it can be unit-tested in plain Node. ConversationProvider does the I/O and dispatches.
import { type ChatResult, type Interrupt, interruptFrom, type StatusEvent, type StreamEvent } from '../api/contract';

export type UiMessage = {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  traceId?: string | null;
  feedbackToken?: string | null;
  tools?: string[];
  feedback?: -1 | 1;
  feedbackError?: string;
  pending?: boolean;
  /** progress line under a pending reply ("Thinking…", "Searching the knowledge base…") — cleared by answer text */
  status?: string;
};

export type TurnKind = 'chat' | 'resume';
export type Notice = { text: string; action: 'retry' | 'new_conversation' };

export type ChatState = {
  sessionId: string; // 1 conversation = 1 session (short-term memory)
  owner: string | null; // user the conversation belongs to (another user signing in ⇒ new conversation)
  messages: UiMessage[];
  pending: Interrupt | null; // approval card
  pendingNote: string | null; // shown on the card (e.g. "the previous attempt may have completed")
  // in-flight / last turn. `ended` once done / interrupt / error / failure was applied: later events are ignored
  turn: { replyId: string; kind: TurnKind; message?: string; resumeOf: Interrupt | null; ended: boolean } | null;
  notice: Notice | null; // bar above the composer
  retry: { message: string } | null; // failed chat message that Retry resends
};

export type ChatAction =
  | { type: 'new_session'; sessionId: string; owner: string | null }
  | { type: 'user_message'; id: string; text: string }
  | { type: 'turn_start'; replyId: string; kind: TurnKind; message?: string }
  | { type: 'event'; replyId: string; event: StreamEvent }
  | { type: 'result'; replyId: string; result: ChatResult }
  | { type: 'failure'; replyId: string; status?: number; message: string; interrupt?: Interrupt }
  | { type: 'closed'; replyId: string } // stream ended without a final event
  | { type: 'stopped'; replyId: string } // user pressed Stop
  | { type: 'feedback'; id: string; score: -1 | 1 | undefined; error?: string }
  | { type: 'dismiss_notice' };

export const TEXT = {
  waiting: 'Waiting for your confirmation…',
  approvalGone:
    'ℹ️ This approval request is no longer pending — it was already handled (possibly by an earlier attempt whose ' +
    'reply did not arrive) or it expired. Nothing was run again. Send a message to continue.',
  approvalResynced: 'ℹ️ The agent is waiting for the approval below.',
  supersededNote: 'The earlier request was already handled — this is the approval the agent is waiting for now.',
  chatBlocked: '⏸ The agent is waiting for your approval below — your message was not sent. Send it again afterwards.',
  stuck:
    '⚠️ This conversation is waiting for an approval that cannot be shown here, so it cannot continue. ' +
    'Start a new conversation.',
  stuckNotice: 'This conversation cannot continue.',
  resumeRetryNote:
    'The previous attempt may have completed on the server. Submitting again is safe: if it already ran, ' +
    'you will be told and nothing runs twice.',
  sessionExpired: '⚠️ Your session has expired — sign in again to continue.',
  closed: 'Connection closed before the answer completed.',
  stopped: '⏹ Stopped.',
  failedNotice: 'The message could not be completed.',
  // SSE `status` phases (translate here); a phase missing from this table shows the backend's `label`
  status: {
    thinking: 'Thinking…',
    tool: 'Using a tool…',
    checking: 'Checking the answer…',
  } as Record<string, string>,
};

/** Status text per tool name (translate / extend for your tools); other tools show the backend's `label`. */
export const TOOL_STATUS: Record<string, string> = {
  search_knowledge: 'Searching the knowledge base…',
  remember: 'Saving to memory…',
  recall_memory: 'Looking through your saved notes…',
  get_current_time: 'Checking the time…',
};

/** The progress line for a `status` event — short, never tool arguments (the event carries none). */
export function statusText(e: Pick<StatusEvent, 'phase' | 'label' | 'tools'>): string {
  if (e.phase === 'tool') {
    const tools = e.tools ?? [];
    const known = tools.map((t) => TOOL_STATUS[t]).filter((t): t is string => !!t);
    if (tools.length && known.length === tools.length) return [...new Set(known)].join(' · ');
    return e.label || TEXT.status.tool;
  }
  return TEXT.status[e.phase] ?? e.label ?? '';
}

export const initialChatState = (sessionId: string, owner: string | null = null): ChatState => ({
  sessionId,
  owner,
  messages: [],
  pending: null,
  pendingNote: null,
  turn: null,
  notice: null,
  retry: null,
});

export const isBusy = (s: ChatState) => !!s.turn && !s.turn.ended;

const patch = (s: ChatState, id: string, fn: (m: UiMessage) => UiMessage): ChatState => ({
  ...s,
  messages: s.messages.map((m) => (m.id === id ? fn(m) : m)),
});

/** The turn `replyId` is current and still open (a late event of an older / finished turn is dropped). */
const open = (s: ChatState, replyId: string) => !!s.turn && s.turn.replyId === replyId && !s.turn.ended;
const end = (s: ChatState): ChatState => (s.turn ? { ...s, turn: { ...s.turn, ended: true } } : s);

// The backend rotates a long session (SESSION_MAX_TURNS): the conversation continues under the id it returns
const nextSessionId = (current: string, returned: unknown) =>
  typeof returned === 'string' && /^[A-Za-z0-9][A-Za-z0-9-]{0,127}$/.test(returned) ? returned : current;

function applyResult(s: ChatState, replyId: string, r: ChatResult): ChatState {
  s = { ...end(s), sessionId: nextSessionId(s.sessionId, r.session_id) };
  if (r.status === 'interrupted') {
    return patch({ ...s, pending: r.interrupt, pendingNote: null, notice: null, retry: null }, replyId, (m) => ({
      ...m,
      text: r.interrupt.message || m.text || TEXT.waiting,
      pending: false,
      status: undefined,
    }));
  }
  return patch({ ...s, notice: null, retry: null }, replyId, (m) => ({
    ...m,
    text: r.response || m.text, // some graphs only stream the answer
    traceId: r.trace_id,
    feedbackToken: r.feedback_token,
    pending: false,
    status: undefined,
  }));
}

function applyFailure(
  s: ChatState,
  replyId: string,
  f: { status?: number; message: string; interrupt?: Interrupt },
): ChatState {
  const turn = s.turn!;
  s = end(s);
  const say = (text: string, keep = true) =>
    patch(s, replyId, (m) => ({
      ...m,
      text: keep && m.text ? `${m.text}\n${text}` : text,
      pending: false,
      status: undefined,
    }));

  if (f.status === 409 && f.interrupt) {
    // An approval IS pending (lost / superseded interrupt): show it again — a dead end otherwise (every chat ⇒ 409)
    s = {
      ...s,
      pending: f.interrupt,
      pendingNote: turn.kind === 'resume' ? TEXT.supersededNote : null,
      notice: null,
      retry: null, // a blocked chat message is put back into the composer by the provider
    };
    return say(turn.kind === 'chat' ? TEXT.chatBlocked : TEXT.approvalResynced, false);
  }
  if (f.status === 409) {
    if (turn.kind === 'resume') {
      // Nothing pending any more (handled by a request whose answer we lost, double submit, expired) — re-showing
      // the card would only 409 again
      s = { ...s, pending: null, pendingNote: null, notice: null, retry: null };
      return say(TEXT.approvalGone, false);
    }
    // chat 409 without the pending approval (backend without the 409+interrupt contract): this session is stuck
    s = { ...s, notice: { text: TEXT.stuckNotice, action: 'new_conversation' }, retry: null };
    return say(TEXT.stuck, false);
  }
  // Transient (network, 5xx, 504, dropped stream, 401): a resume may or may not have run ⇒ show the card again with
  // a note; resubmitting is safe (interrupt_id ⇒ 409, then the branches above re-sync)
  if (turn.kind === 'resume' && turn.resumeOf) {
    s = { ...s, pending: turn.resumeOf, pendingNote: TEXT.resumeRetryNote, notice: null, retry: null };
  } else if (turn.kind === 'chat' && turn.message) {
    s = { ...s, notice: { text: TEXT.failedNotice, action: 'retry' }, retry: { message: turn.message } };
  }
  return say(f.status === 401 ? TEXT.sessionExpired : `⚠️ ${f.message}`);
}

export function chatReducer(s: ChatState, a: ChatAction): ChatState {
  switch (a.type) {
    case 'new_session':
      return initialChatState(a.sessionId, a.owner);
    case 'user_message':
      return { ...s, messages: [...s.messages, { id: a.id, role: 'user', text: a.text }] };
    case 'turn_start':
      if (isBusy(s)) return s; // one turn at a time
      return {
        ...s,
        // the card is hidden while its resume runs; kept in `resumeOf` to show it again if the resume fails
        pending: null,
        pendingNote: null,
        notice: null,
        retry: null,
        turn: {
          replyId: a.replyId,
          kind: a.kind,
          message: a.message,
          resumeOf: a.kind === 'resume' ? s.pending : null,
          ended: false,
        },
        // "Thinking…" at once, streaming or not; a streaming backend refines it with `status` events
        messages: [
          ...s.messages,
          { id: a.replyId, role: 'assistant', text: '', pending: true, tools: [], status: TEXT.status.thinking },
        ],
      };
    case 'event': {
      if (!open(s, a.replyId)) return s;
      const e = a.event;
      switch (e.event) {
        case 'token': // answer text replaces the progress line
          return patch(s, a.replyId, (m) => ({ ...m, text: m.text + e.data, status: undefined }));
        case 'reset': // self-eval retry / LLM fallback restarts the answer: drop what was shown, tools included
          return patch(s, a.replyId, (m) => ({ ...m, text: '', tools: [], status: undefined }));
        case 'status':
          return patch(s, a.replyId, (m) => ({ ...m, status: statusText(e) || m.status }));
        case 'tool_start':
          return patch(s, a.replyId, (m) => ({ ...m, tools: [...(m.tools ?? []), e.name] }));
        case 'interrupt':
        case 'done':
          return applyResult(s, a.replyId, e as unknown as ChatResult);
        case 'error': // in-stream errors carry the HTTP-like status (409, 504…); 409 may carry the pending approval
          return applyFailure(s, a.replyId, {
            status: e.status,
            message: e.message || 'Agent error',
            interrupt: interruptFrom(e),
          });
        default:
          return s;
      }
    }
    case 'result':
      return open(s, a.replyId) ? applyResult(s, a.replyId, a.result) : s;
    case 'failure':
      return open(s, a.replyId) ? applyFailure(s, a.replyId, a) : s;
    case 'closed':
      return open(s, a.replyId) ? applyFailure(s, a.replyId, { message: TEXT.closed }) : s;
    case 'stopped': {
      if (!open(s, a.replyId)) return s;
      const turn = s.turn!;
      s = patch(end(s), a.replyId, (m) => ({
        ...m,
        text: m.text ? `${m.text}\n${TEXT.stopped}` : TEXT.stopped,
        pending: false,
        status: undefined,
      }));
      // a stopped resume may still complete on the server ⇒ keep the card (resubmitting re-syncs via 409)
      return turn.kind === 'resume' && turn.resumeOf ? { ...s, pending: turn.resumeOf, pendingNote: TEXT.resumeRetryNote } : s;
    }
    case 'feedback':
      return patch(s, a.id, (m) => ({ ...m, feedback: a.score, feedbackError: a.error }));
    case 'dismiss_notice':
      return { ...s, notice: null };
  }
}
