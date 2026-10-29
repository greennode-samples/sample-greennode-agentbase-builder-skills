'use client';
// One chat message. Model output is rendered as TEXT nodes (React escapes it) — never dangerouslySetInnerHTML, never
// a markdown-to-HTML renderer without a sanitizer. Links: linkify() (shared with the Expo app) finds https URLs only;
// they open in a new tab with rel="noopener noreferrer" (the agent's page gets no window.opener, no Referer).
import React from 'react';

import type { UiMessage } from '../chat/chatState';
import { useStrings } from '../lib/LocaleProvider';
import { linkify } from './linkify';

type Props = { message: UiMessage; onFeedback?: (score: -1 | 1) => void };

const SAFE_URL = /^https:\/\/[^\s<>"'`]+$/i; // defense in depth: linkify already returns https URLs only

export function MessageBubble({ message: m, onFeedback }: Props) {
  const t = useStrings();
  const canRate = m.role === 'assistant' && !m.pending && !!m.traceId && !!m.feedbackToken;
  return (
    <div className={`bubble ${m.role}`}>
      {!!m.tools?.length && (
        <div className="tools small" aria-label={t.toolsUsed}>
          🔧 {m.tools.join(', ')}
        </div>
      )}
      <div className="text">
        <span className="sr-only">{m.role === 'user' ? t.you : t.agent}: </span>
        {linkify(m.text).map((p, i) =>
          p.url && SAFE_URL.test(p.url) ? (
            <a key={i} href={p.url} target="_blank" rel="noopener noreferrer nofollow">
              {p.text}
            </a>
          ) : (
            <React.Fragment key={i}>{p.text}</React.Fragment>
          ),
        )}
      </div>
      {m.pending && !!m.status && (
        <div className="status small">
          {m.status}
        </div>
      )}
      {canRate && (
        <div className="row feedback">
          {([1, -1] as const).map((score) => (
            <button
              key={score}
              type="button"
              aria-label={score === 1 ? t.goodAnswer : t.badAnswer}
              aria-pressed={m.feedback === score}
              className={`icon ${m.feedback === score ? 'selected' : ''}`}
              onClick={() => onFeedback?.(score)}
            >
              {score === 1 ? '👍' : '👎'}
            </button>
          ))}
          {!!m.feedbackError && <span className="error small">{m.feedbackError}</span>}
        </div>
      )}
    </div>
  );
}
