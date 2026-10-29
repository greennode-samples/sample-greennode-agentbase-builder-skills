import { describe, expect, it } from 'vitest';

import type { Interrupt } from '../src/api/contract';
import { type ChatState, chatReducer, initialChatState, isBusy, statusText, TEXT } from '../src/chat/chatState';

const card = (id: string): Interrupt => ({
  id,
  type: 'tool_approval',
  message: 'Create the ticket?',
  tool_calls: [{ id: 'call_1', name: 'create_ticket', args: { title: 'x' } }],
});

const start = (s: ChatState, kind: 'chat' | 'resume' = 'chat', replyId = 'r1') =>
  chatReducer(s, { type: 'turn_start', replyId, kind, message: kind === 'chat' ? 'hi' : undefined });

const reply = (s: ChatState, id = 'r1') => s.messages.find((m) => m.id === id)!;

describe('chatReducer', () => {
  it('streams tokens, clears the status line, stores trace + feedback token on done', () => {
    let s = start(initialChatState('s1', 'u1'));
    expect(reply(s).status).toBe(TEXT.status.thinking);
    s = chatReducer(s, { type: 'event', replyId: 'r1', event: { event: 'status', phase: 'tool', tools: ['remember'] } });
    expect(reply(s).status).toBe('Saving to memory…');
    s = chatReducer(s, { type: 'event', replyId: 'r1', event: { event: 'token', data: 'Hel' } });
    s = chatReducer(s, { type: 'event', replyId: 'r1', event: { event: 'token', data: 'lo' } });
    expect(reply(s)).toMatchObject({ text: 'Hello', status: undefined });
    s = chatReducer(s, {
      type: 'event',
      replyId: 'r1',
      event: { event: 'done', status: 'success', response: '', tools_used: [], session_id: 's1', trace_id: 't', feedback_token: 'f' },
    });
    expect(reply(s)).toMatchObject({ text: 'Hello', traceId: 't', feedbackToken: 'f', pending: false });
    expect(isBusy(s)).toBe(false);
  });

  it('reset clears text and tools', () => {
    let s = start(initialChatState('s1'));
    s = chatReducer(s, { type: 'event', replyId: 'r1', event: { event: 'tool_start', name: 'search' } });
    s = chatReducer(s, { type: 'event', replyId: 'r1', event: { event: 'token', data: 'draft' } });
    s = chatReducer(s, { type: 'event', replyId: 'r1', event: { event: 'reset', reason: 'self_eval_retry' } });
    expect(reply(s)).toMatchObject({ text: '', tools: [] });
  });

  it('nothing after the final event changes the result', () => {
    let s = start(initialChatState('s1'));
    s = chatReducer(s, { type: 'event', replyId: 'r1', event: { event: 'interrupt', status: 'interrupted', interrupt: card('i1'), session_id: 's1', trace_id: null, feedback_token: null } });
    const after = chatReducer(s, { type: 'event', replyId: 'r1', event: { event: 'error', message: 'late', status: 500 } });
    expect(after).toBe(s);
    expect(s.pending?.id).toBe('i1');
  });

  it('409 + interrupt on a chat re-shows that approval card', () => {
    let s = start(initialChatState('s1'));
    s = chatReducer(s, { type: 'failure', replyId: 'r1', status: 409, message: 'pending', interrupt: card('i2') });
    expect(s.pending?.id).toBe('i2');
    expect(reply(s).text).toBe(TEXT.chatBlocked);
  });

  it('409 without interrupt on a resume drops the card', () => {
    let s: ChatState = { ...initialChatState('s1'), pending: card('i1') };
    s = start(s, 'resume');
    expect(s.pending).toBeNull();
    s = chatReducer(s, { type: 'failure', replyId: 'r1', status: 409, message: 'nothing pending' });
    expect(s.pending).toBeNull();
    expect(reply(s).text).toBe(TEXT.approvalGone);
  });

  it('a failed resume shows the card again with the retry note', () => {
    let s: ChatState = { ...initialChatState('s1'), pending: card('i1') };
    s = start(s, 'resume');
    s = chatReducer(s, { type: 'closed', replyId: 'r1' });
    expect(s.pending?.id).toBe('i1');
    expect(s.pendingNote).toBe(TEXT.resumeRetryNote);
  });

  it('a failed chat offers Retry; 401 says the session expired', () => {
    let s = start(initialChatState('s1'));
    s = chatReducer(s, { type: 'failure', replyId: 'r1', status: 401, message: 'x' });
    expect(s.retry).toEqual({ message: 'hi' });
    expect(s.notice?.action).toBe('retry');
    expect(reply(s).text).toBe(TEXT.sessionExpired);
  });

  it('ignores events of an older turn and allows one turn at a time', () => {
    let s = start(initialChatState('s1'));
    expect(start(s, 'chat', 'r2')).toBe(s);
    s = chatReducer(s, { type: 'event', replyId: 'old', event: { event: 'token', data: 'x' } });
    expect(reply(s).text).toBe('');
  });

  it('statusText falls back to the backend label for unknown phases / tools', () => {
    expect(statusText({ phase: 'planning', label: 'Planning…' })).toBe('Planning…');
    expect(statusText({ phase: 'tool', tools: ['unknown_tool'], label: 'Using unknown_tool…' })).toBe('Using unknown_tool…');
  });
});
