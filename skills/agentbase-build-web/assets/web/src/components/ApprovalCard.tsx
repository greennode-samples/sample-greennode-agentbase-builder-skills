'use client';
// Human-in-the-loop approval card: shows the pending tool calls; per call Approve / Edit (JSON args) / Reject (reason).
// Render it with `key={interrupt.id}` so its local state resets for every new interrupt. Tool names, args and the
// message are rendered as TEXT (React escapes them) — never as HTML.
import React, { useState } from 'react';

import type { Decision, Interrupt, PendingToolCall } from '../api/contract';
import { type Action, parseArgs, pretty, toDecision } from './approval';

type Props = {
  interrupt: Interrupt;
  onSubmit: (decisions: Decision[]) => void;
  busy?: boolean;
  /** e.g. "the previous attempt may have completed" after a failed resume */
  note?: string | null;
};

const LABEL: Record<Action, string> = { approve: 'Approve', edit: 'Edit', reject: 'Reject' };

export function ApprovalCard({ interrupt, onSubmit, busy, note }: Props) {
  const [choice, setChoice] = useState<Record<string, Action>>({});
  const [edited, setEdited] = useState<Record<string, string>>({});
  const [reasons, setReasons] = useState<Record<string, string>>({});

  const editText = (tc: PendingToolCall) => edited[tc.id] ?? pretty(tc.args);
  const editError = (tc: PendingToolCall) => (choice[tc.id] === 'edit' ? parseArgs(editText(tc)).error : undefined);
  const complete = interrupt.tool_calls.every((tc) => choice[tc.id] && !editError(tc));
  const disabled = !complete || !!busy;

  return (
    <section className="approval" aria-label="Approval needed">
      <h2>Your confirmation is needed</h2>
      {!!interrupt.message && <p className="approval-msg">{interrupt.message}</p>}
      {!!note && <p className="approval-note">{note}</p>}
      {interrupt.tool_calls.map((tc) => {
        const err = editError(tc);
        return (
          <div key={tc.id} className="approval-call">
            <div className="mono strong">{tc.name}</div>
            {choice[tc.id] !== 'edit' && <pre className="mono small">{pretty(tc.args)}</pre>}
            <div role="radiogroup" aria-label={`Decision for ${tc.name}`} className="row">
              {(['approve', 'edit', 'reject'] as const).map((a) => (
                <button
                  key={a}
                  type="button"
                  role="radio"
                  aria-checked={choice[tc.id] === a}
                  disabled={busy}
                  className={`choice ${choice[tc.id] === a ? `selected ${a}` : ''}`}
                  onClick={() => setChoice((c) => ({ ...c, [tc.id]: a }))}
                >
                  {LABEL[a]}
                </button>
              ))}
            </div>
            {choice[tc.id] === 'edit' && (
              <>
                <textarea
                  className={`mono small json ${err ? 'invalid' : ''}`}
                  aria-label={`Arguments for ${tc.name} (JSON)`}
                  value={editText(tc)}
                  onChange={(e) => setEdited((x) => ({ ...x, [tc.id]: e.target.value }))}
                  spellCheck={false}
                  disabled={busy}
                />
                {!!err && <p className="error small">{err}</p>}
              </>
            )}
            {choice[tc.id] === 'reject' && (
              <input
                aria-label={`Reason for rejecting ${tc.name}`}
                placeholder="Reason for rejecting (optional)"
                value={reasons[tc.id] ?? ''}
                maxLength={1000}
                onChange={(e) => setReasons((r) => ({ ...r, [tc.id]: e.target.value }))}
                disabled={busy}
              />
            )}
          </div>
        );
      })}
      <button
        type="button"
        className="primary"
        disabled={disabled}
        onClick={() => onSubmit(interrupt.tool_calls.map((tc) => toDecision(tc, choice[tc.id], edited[tc.id], reasons[tc.id])))}
      >
        Submit decision
      </button>
    </section>
  );
}
