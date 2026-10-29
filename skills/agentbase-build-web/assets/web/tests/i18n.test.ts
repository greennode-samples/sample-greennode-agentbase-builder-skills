import { afterEach, describe, expect, it } from 'vitest';

import { chatReducer, initialChatState, statusText, TEXT } from '../src/chat/chatState';
import { announcement } from '../src/components/announce';
import { applyChatText, LOCALES, resolveLocale, STRINGS } from '../src/lib/i18n';

afterEach(() => applyChatText('en'));

/** key paths of a dictionary, with the kind of each leaf — two languages must have the same shape */
function shape(value: unknown, path = ''): string[] {
  if (value && typeof value === 'object') return Object.entries(value).flatMap(([k, v]) => shape(v, `${path}.${k}`));
  return [`${path}:${typeof value}`];
}

describe('languages', () => {
  it('every language has every string, and none is empty', () => {
    const [first, ...others] = LOCALES;
    for (const other of others) expect(shape(STRINGS[other]).sort()).toEqual(shape(STRINGS[first!]).sort());
    for (const locale of LOCALES) {
      const flat = JSON.stringify(STRINGS[locale], (_k, v) => (typeof v === 'function' ? v('x') : v));
      expect(flat).not.toMatch(/:""/);
      expect(STRINGS[locale].decisionFor('transfer')).toContain('transfer');
    }
  });

  it('resolves the language: an explicit setting wins, auto follows the browser, else English', () => {
    expect(resolveLocale('vi', ['en-US'])).toBe('vi');
    expect(resolveLocale('EN', ['vi'])).toBe('en');
    expect(resolveLocale('auto', ['fr-FR', 'vi-VN', 'en'])).toBe('vi');
    expect(resolveLocale(undefined, ['vi'])).toBe('vi');
    expect(resolveLocale('auto', ['fr', 'de'])).toBe('en');
    expect(resolveLocale('klingon', [])).toBe('en');
  });

  it('translates the words of the shared chat logic in place, and restores them', () => {
    const english = TEXT.stopped;
    applyChatText('vi');
    expect(TEXT.stopped).not.toBe(english);
    expect(statusText({ phase: 'thinking' })).toMatch(/suy nghĩ/);
    expect(statusText({ phase: 'tool', tools: ['remember'] })).toMatch(/bộ nhớ/);
    applyChatText('en');
    expect(TEXT.stopped).toBe(english);
    expect(statusText({ phase: 'tool', tools: ['remember'] })).toBe('Saving to memory…');
  });
});

describe('what a screen reader is told', () => {
  const t = STRINGS.en;
  const start = () =>
    chatReducer(chatReducer(initialChatState('s1'), { type: 'user_message', id: 'u1', text: 'hi' }), {
      type: 'turn_start',
      replyId: 'r1',
      kind: 'chat',
      message: 'hi',
    });

  it('says nothing while an answer streams, then the finished answer once', () => {
    let s = start();
    expect(announcement(s, t)).toBe('');
    s = chatReducer(s, { type: 'event', replyId: 'r1', event: { event: 'token', data: 'Hel' } });
    s = chatReducer(s, { type: 'event', replyId: 'r1', event: { event: 'token', data: 'lo' } });
    expect(announcement(s, t)).toBe('');
    s = chatReducer(s, {
      type: 'event',
      replyId: 'r1',
      event: { event: 'done', status: 'success', response: 'Hello', tools_used: [], session_id: 's1', trace_id: 't' } as never,
    });
    expect(announcement(s, t)).toBe('Agent: Hello');
  });

  it('announces a pending approval, and cuts a very long answer', () => {
    const card = { id: 'i1', type: 'tool_approval', message: '', tool_calls: [{ id: 'c1', name: 'x', args: {} }] };
    let s = chatReducer(start(), {
      type: 'event',
      replyId: 'r1',
      event: { event: 'interrupt', status: 'interrupted', interrupt: card, session_id: 's1' } as never,
    });
    expect(announcement(s, t)).toBe(t.approvalAnnouncement);
    s = chatReducer(initialChatState('s2'), { type: 'user_message', id: 'u1', text: 'q' });
    s = { ...s, messages: [...s.messages, { id: 'a1', role: 'assistant', text: 'x'.repeat(2000) }] };
    expect(announcement(s, t).length).toBeLessThan(700);
    expect(announcement(s, t).endsWith('…')).toBe(true);
    expect(announcement(initialChatState('s3'), t)).toBe('');
  });
});
