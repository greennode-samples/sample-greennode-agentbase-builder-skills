import { describe, expect, it } from 'vitest';

import { parseArgs, toDecision } from '../src/components/approval';

const tc = { id: 'c1', name: 'transfer', args: { amount: 10 } };

describe('approval decisions', () => {
  it('validates edited JSON', () => {
    expect(parseArgs('{"a":1}').args).toEqual({ a: 1 });
    expect(parseArgs('[1]').error).toMatch(/JSON object/);
    expect(parseArgs('{').error).toMatch(/Invalid JSON/);
  });
  it('unchanged edit ⇒ approve, changed ⇒ edit with args', () => {
    expect(toDecision(tc, 'edit', undefined, undefined)).toEqual({ tool_call_id: 'c1', action: 'approve' });
    expect(toDecision(tc, 'edit', '{"amount": 5}', undefined)).toEqual({ tool_call_id: 'c1', action: 'edit', args: { amount: 5 } });
  });
  it('reject always carries a reason', () => {
    expect(toDecision(tc, 'reject', undefined, '  ')).toMatchObject({ reason: 'Rejected by the user' });
    expect(toDecision(tc, 'reject', undefined, 'wrong account')).toMatchObject({ reason: 'wrong account' });
  });
});
