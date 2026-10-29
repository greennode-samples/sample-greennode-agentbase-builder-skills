'use client';
// Header (New / Sign out), messages, approval card, notice (Retry / Start new conversation), composer (Send / Stop).
import React, { useEffect, useRef } from 'react';

import { useAuth } from '../auth/AuthProvider';
import { useConversation } from '../chat/ConversationProvider';
import { ApprovalCard } from '../components/ApprovalCard';
import { announcement } from '../components/announce';
import { MessageBubble } from '../components/MessageBubble';
import { useStrings } from '../lib/LocaleProvider';

export function ChatScreen() {
  const { userName, userId } = useAuth();
  const c = useConversation();
  const { state, busy } = c;
  const t = useStrings();
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    end.current?.scrollIntoView({ block: 'end' });
  }, [state.messages, state.pending]);

  return (
    <div className="chat">
      <header className="row header">
        <strong className="grow">__PROJECT_NAME__</strong>
        <span className="small muted">{userName ?? userId}</span>
        <button type="button" onClick={c.newConversation}>
          {t.newConversation}
        </button>
        <button type="button" onClick={c.signOut}>
          {t.signOut}
        </button>
      </header>
      {/* Not live (an answer changes on every token): the finished answer is announced once, below */}
      <main className="messages" role="log" aria-live="off" aria-label={t.conversation}>
        {state.messages.map((m) => (
          <MessageBubble key={m.id} message={m} onFeedback={(score) => c.feedback(m, score)} />
        ))}
        {state.pending && (
          <ApprovalCard
            key={state.pending.id ?? 'pending'}
            interrupt={state.pending}
            note={state.pendingNote}
            busy={busy}
            onSubmit={c.decide}
          />
        )}
        <div ref={end} />
      </main>
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {announcement(state, t)}
      </div>
      {state.notice && (
        <div className="row notice" role="status">
          <span className="grow">{state.notice.text}</span>
          {state.notice.action === 'retry' ? (
            <button type="button" onClick={c.retry} disabled={busy}>
              {t.retry}
            </button>
          ) : (
            <button type="button" onClick={c.newConversation}>
              {t.startNew}
            </button>
          )}
          <button type="button" aria-label={t.dismiss} onClick={c.dismissNotice}>
            ✕
          </button>
        </div>
      )}
      <form
        className="row composer"
        onSubmit={(e) => {
          e.preventDefault();
          c.send();
        }}
      >
        <textarea
          className="grow"
          aria-label={t.message}
          placeholder={state.pending ? t.answerApprovalFirst : t.message}
          value={c.input}
          rows={2}
          onChange={(e) => c.setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              c.send();
            }
          }}
        />
        {busy ? (
          <button type="button" onClick={c.stop}>
            {t.stop}
          </button>
        ) : (
          <button type="submit" className="primary" disabled={!c.input.trim() || !!state.pending}>
            {t.send}
          </button>
        )}
      </form>
    </div>
  );
}
