'use client';
// Conversation I/O: runs turns (chat / resume, streaming or not) and dispatches to the pure chatReducer (byte-identical
// to the Expo app's). Adapted from the Expo ConversationProvider: no token handling here — the BFF refreshes the
// access token and retries a 401 itself, so a 401 reaching the browser means "sign in again".
import React, { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, useState } from 'react';

import {
  AgentError,
  type Body,
  chatBody,
  type Decision,
  invoke,
  invokeStream,
  isTerminal,
  resumeBody,
  sendFeedback,
  type StreamEvent,
} from '../api/agentClient';
import { useAuth } from '../auth/AuthProvider';
import { env } from '../config/env';
import { type ChatState, chatReducer, initialChatState, isBusy, type TurnKind, type UiMessage } from './chatState';

// 503 = MaaS / Memory overloaded, JWKS / IdP unreachable, answered BEFORE the turn runs ⇒ retry with backoff
const RETRY_503_MS = [1000, 3000];

type Conversation = {
  state: ChatState;
  busy: boolean;
  input: string;
  setInput: (text: string) => void;
  send: () => void;
  retry: () => void;
  decide: (decisions: Decision[]) => void;
  stop: () => void;
  newConversation: () => void;
  /** user sign-out: clears the conversation, then ends the BFF + IdP session */
  signOut: () => void;
  feedback: (m: UiMessage, score: -1 | 1) => void;
  dismissNotice: () => void;
};

const ConversationContext = createContext<Conversation | null>(null);

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    if (signal.aborted) return reject(new Error('aborted'));
    const t = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(t);
      reject(new Error('aborted'));
    };
    signal.addEventListener('abort', onAbort);
  });

const newId = () => crypto.randomUUID(); // secure context (https or localhost) — required anyway for Secure cookies

export function ConversationProvider({ children }: { children: React.ReactNode }) {
  const auth = useAuth();
  const { userId, csrfToken } = auth;
  // The session id is created in the browser (not during server rendering)
  const [state, dispatch] = useReducer(chatReducer, undefined, () => initialChatState(''));
  const [input, setInput] = useState('');
  const stateRef = useRef(state);
  stateRef.current = state;
  // Bumped by "New" / sign-out / user change: callbacks of an older request are ignored (no late interrupt card)
  const generation = useRef(0);
  const inflight = useRef<{ ctrl: AbortController; stopped: boolean } | null>(null);

  const reset = useCallback((owner: string | null) => {
    generation.current += 1;
    inflight.current?.ctrl.abort();
    inflight.current = null;
    dispatch({ type: 'new_session', sessionId: newId(), owner });
  }, []);

  // First mount / another user signed in ⇒ a new conversation (never show a user the previous user's messages)
  useEffect(() => {
    const s = stateRef.current;
    if (!s.sessionId || (userId && userId !== s.owner)) {
      reset(userId);
      if (s.sessionId) setInput('');
    }
  }, [userId, reset]);

  const run = useCallback(
    async (body: Body, kind: TurnKind, message?: string) => {
      if (inflight.current || isBusy(stateRef.current)) return; // one turn at a time (also within one frame)
      const gen = generation.current;
      const live = () => gen === generation.current;
      const { sessionId } = stateRef.current;
      const replyId = newId();
      const me = { ctrl: new AbortController(), stopped: false };
      inflight.current = me;
      dispatch({ type: 'turn_start', replyId, kind, message });
      let ended = false; // a final event was applied — nothing after it (late transport error…) may change the result
      const afterFailure = (status: number | undefined) => {
        if (status === 401) auth.expired(); // the BFF already tried a refresh: sign in again
        if (kind === 'chat' && message) setInput((cur) => cur || message); // give the text back to edit / resend
      };
      const emit = (event: StreamEvent) => {
        if (!live()) return;
        if (isTerminal(event)) ended = true;
        dispatch({ type: 'event', replyId, event });
        if (event.event === 'error') afterFailure(event.status);
      };
      try {
        for (let attempt = 0; ; attempt++) {
          try {
            const ctx = { sessionId, csrfToken };
            const opts = { signal: me.ctrl.signal, idleTimeoutMs: env.idleTimeoutMs };
            if (env.streaming) {
              await invokeStream(body, ctx, emit, opts);
            } else {
              const result = await invoke(body, ctx, opts);
              if (live()) {
                ended = true;
                dispatch({ type: 'result', replyId, result });
              }
            }
            break;
          } catch (err) {
            // Only HTTP errors answered before the turn ran are retried (nothing streamed yet)
            if (ended || !live() || me.ctrl.signal.aborted || !(err instanceof AgentError)) throw err;
            if (err.status === 503 && attempt < RETRY_503_MS.length) {
              await sleep(RETRY_503_MS[attempt], me.ctrl.signal);
              continue;
            }
            throw err;
          }
        }
        if (!live()) return;
        if (me.stopped) dispatch({ type: 'stopped', replyId });
        else if (!ended) dispatch({ type: 'closed', replyId }); // stream closed without a final event
      } catch (err) {
        if (!live()) return; // "New" / sign-out replaced the conversation
        if (ended) return; // the result already arrived: a transport error while closing must not overwrite it
        if (me.stopped) {
          dispatch({ type: 'stopped', replyId });
          return;
        }
        const status = err instanceof AgentError ? err.status : undefined;
        const interrupt = err instanceof AgentError ? err.interrupt : undefined;
        const text = err instanceof Error ? err.message : String(err);
        dispatch({ type: 'failure', replyId, status, message: text, interrupt });
        afterFailure(status);
      } finally {
        if (inflight.current === me) inflight.current = null;
      }
    },
    [csrfToken, auth],
  );

  const send = useCallback(() => {
    const text = input.trim();
    const s = stateRef.current;
    if (!text || !s.sessionId || inflight.current || isBusy(s) || s.pending) return; // inflight: a 2nd click
    setInput('');
    dispatch({ type: 'user_message', id: newId(), text });
    void run(chatBody(text), 'chat', text);
  }, [input, run]);

  const retry = useCallback(() => {
    const s = stateRef.current;
    if (!s.retry || inflight.current || isBusy(s) || s.pending) return;
    const text = s.retry.message;
    setInput((cur) => (cur.trim() === text ? '' : cur));
    void run(chatBody(text), 'chat', text);
  }, [run]);

  const decide = useCallback(
    (decisions: Decision[]) => {
      const s = stateRef.current;
      if (!s.pending || inflight.current || isBusy(s)) return;
      void run(resumeBody(decisions, s.pending.id), 'resume');
    },
    [run],
  );

  // Stop: abort this turn, keep the session (a stopped resume shows its card again)
  const stop = useCallback(() => {
    const cur = inflight.current;
    if (!cur) return;
    cur.stopped = true;
    cur.ctrl.abort();
  }, []);

  // New conversation = new session_id (new short-term memory; long-term memory persists). The composer text is kept.
  const newConversation = useCallback(() => reset(stateRef.current.owner), [reset]);

  const signOut = useCallback(() => {
    reset(null);
    setInput('');
    void auth.signOut();
  }, [reset, auth]);

  const feedback = useCallback(
    async (m: UiMessage, score: -1 | 1) => {
      if (!m.traceId || !m.feedbackToken) return;
      const previous = m.feedback;
      dispatch({ type: 'feedback', id: m.id, score });
      try {
        await sendFeedback(m.traceId, m.feedbackToken, score, { sessionId: stateRef.current.sessionId, csrfToken });
      } catch (err) {
        // not recorded ⇒ don't show it as recorded
        const why = err instanceof AgentError && err.status === 403 ? 'not allowed' : 'try again';
        dispatch({ type: 'feedback', id: m.id, score: previous, error: `Feedback not sent (${why})` });
        if (err instanceof AgentError && err.status === 401) auth.expired();
      }
    },
    [csrfToken, auth],
  );

  const value = useMemo<Conversation>(
    () => ({
      state,
      busy: isBusy(state),
      input,
      setInput,
      send,
      retry,
      decide,
      stop,
      newConversation,
      signOut,
      feedback: (m, score) => void feedback(m, score),
      dismissNotice: () => dispatch({ type: 'dismiss_notice' }),
    }),
    [state, input, send, retry, decide, stop, newConversation, signOut, feedback],
  );

  return <ConversationContext.Provider value={value}>{children}</ConversationContext.Provider>;
}

export function useConversation(): Conversation {
  const ctx = useContext(ConversationContext);
  if (!ctx) throw new Error('useConversation must be used inside ConversationProvider');
  return ctx;
}
