'use client';
// Header (New / Sign out), messages, approval card, notice (Retry / Start new conversation), composer (Send / Stop).
import React, { useEffect, useRef } from 'react';

import { useAuth } from '../auth/AuthProvider';
import { useConversation } from '../chat/ConversationProvider';
import { ApprovalCard } from '../components/ApprovalCard';
import { MessageBubble } from '../components/MessageBubble';

export function ChatScreen() {
  const { userName, userId } = useAuth();
  const c = useConversation();
  const { state, busy } = c;
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
          New
        </button>
        <button type="button" onClick={c.signOut}>
          Sign out
        </button>
      </header>
      <main className="messages" aria-live="polite">
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
      {state.notice && (
        <div className="row notice" role="status">
          <span className="grow">{state.notice.text}</span>
          {state.notice.action === 'retry' ? (
            <button type="button" onClick={c.retry} disabled={busy}>
              Retry
            </button>
          ) : (
            <button type="button" onClick={c.newConversation}>
              Start new conversation
            </button>
          )}
          <button type="button" aria-label="Dismiss" onClick={c.dismissNotice}>
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
          aria-label="Message"
          placeholder={state.pending ? 'Answer the approval above first' : 'Message'}
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
            Stop
          </button>
        ) : (
          <button type="submit" className="primary" disabled={!c.input.trim() || !!state.pending}>
            Send
          </button>
        )}
      </form>
    </div>
  );
}
