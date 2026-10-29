import { describe, expect, it } from 'vitest';

import { chatBody, interruptFrom, isTerminal, resumeBody } from '../src/api/contract';

describe('contract', () => {
  it('builds chat / resume bodies (resume always carries interrupt_id)', () => {
    expect(chatBody('hi')).toEqual({ type: 'chat', message: 'hi' });
    expect(resumeBody([{ tool_call_id: 'c', action: 'approve' }])).toEqual({
      type: 'resume',
      decisions: [{ tool_call_id: 'c', action: 'approve' }],
      interrupt_id: null,
    });
  });
  it('reads the pending interrupt from an SSE error and from an HTTP 409 body', () => {
    const it1 = { id: 'i', message: 'm', tool_calls: [{ id: 'c', name: 'n', args: { a: 1 } }] };
    expect(interruptFrom({ interrupt: it1 })?.id).toBe('i');
    expect(interruptFrom({ error: 'x', details: { interrupt: it1 } })?.tool_calls[0].args).toEqual({ a: 1 });
    expect(interruptFrom({ details: null })).toBeUndefined();
    expect(interruptFrom({ interrupt: { tool_calls: [] } })).toBeUndefined();
  });
  it('done / interrupt / error are terminal', () => {
    expect(['done', 'interrupt', 'error', 'token'].map((event) => isTerminal({ event }))).toEqual([true, true, true, false]);
  });
});
