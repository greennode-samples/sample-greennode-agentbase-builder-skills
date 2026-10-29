// Decision logic of the approval card — pure (no React / DOM imports), unit-tested in tests/approval.test.ts.
// Same rules as the Expo app's ApprovalCard: edited args must be a JSON object; unchanged args ⇒ a plain approval;
// a rejection always carries a reason.
import type { Decision, PendingToolCall } from '../api/contract';

export type Action = Decision['action'];

export const pretty = (args: unknown) => JSON.stringify(args ?? {}, null, 2);

/** Edited JSON ⇒ args object, or the reason it is not valid. The backend validates against the tool schema too. */
export function parseArgs(text: string): { args?: Record<string, unknown>; error?: string } {
  try {
    const v: unknown = JSON.parse(text);
    if (!v || typeof v !== 'object' || Array.isArray(v)) return { error: 'Arguments must be a JSON object { … }' };
    return { args: v as Record<string, unknown> };
  } catch (e) {
    return { error: `Invalid JSON: ${e instanceof Error ? e.message : String(e)}` };
  }
}

export function toDecision(tc: PendingToolCall, action: Action, editedText: string | undefined, reason: string | undefined): Decision {
  if (action === 'edit') {
    const args = parseArgs(editedText ?? pretty(tc.args)).args;
    if (!args) throw new Error(`invalid arguments for ${tc.name}`);
    // unchanged ⇒ a plain approval (keeps the trace readable)
    return JSON.stringify(args) === JSON.stringify(tc.args)
      ? { tool_call_id: tc.id, action: 'approve' }
      : { tool_call_id: tc.id, action: 'edit', args };
  }
  if (action === 'reject') return { tool_call_id: tc.id, action: 'reject', reason: reason?.trim() || 'Rejected by the user' };
  return { tool_call_id: tc.id, action: 'approve' };
}
