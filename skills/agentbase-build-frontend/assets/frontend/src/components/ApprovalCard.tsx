// Human-in-the-loop approval card: shows the pending tool calls; per call Approve / Edit (JSON args) / Reject (reason).
// Render it with `key={interrupt.id}` so its local state resets for every new interrupt.
import React, { useState } from 'react';
import { Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import type { Decision, Interrupt, PendingToolCall } from '../api/agentClient';

type Action = Decision['action'];
type Props = {
  interrupt: Interrupt;
  onSubmit: (decisions: Decision[]) => void;
  busy?: boolean;
  /** e.g. "the previous attempt may have completed" after a failed resume */
  note?: string | null;
};

const LABEL: Record<Action, string> = { approve: 'Approve', edit: 'Edit', reject: 'Reject' };
const pretty = (args: unknown) => JSON.stringify(args ?? {}, null, 2);

/** Edited JSON ⇒ args object, or the reason it is not valid. The backend validates against the tool schema too. */
function parseArgs(text: string): { args?: Record<string, unknown>; error?: string } {
  try {
    const v: unknown = JSON.parse(text);
    if (!v || typeof v !== 'object' || Array.isArray(v)) return { error: 'Arguments must be a JSON object { … }' };
    return { args: v as Record<string, unknown> };
  } catch (e) {
    return { error: `Invalid JSON: ${e instanceof Error ? e.message : String(e)}` };
  }
}

export function ApprovalCard({ interrupt, onSubmit, busy, note }: Props) {
  const [choice, setChoice] = useState<Record<string, Action>>({});
  const [edited, setEdited] = useState<Record<string, string>>({});
  const [reasons, setReasons] = useState<Record<string, string>>({});

  const editText = (tc: PendingToolCall) => edited[tc.id] ?? pretty(tc.args);
  const editError = (tc: PendingToolCall) => (choice[tc.id] === 'edit' ? parseArgs(editText(tc)).error : undefined);
  const complete = interrupt.tool_calls.every((tc) => choice[tc.id] && !editError(tc));

  const decision = (tc: PendingToolCall): Decision => {
    const action = choice[tc.id];
    if (action === 'edit') {
      const args = parseArgs(editText(tc)).args!;
      // unchanged ⇒ a plain approval (keeps the trace readable)
      return JSON.stringify(args) === JSON.stringify(tc.args)
        ? { tool_call_id: tc.id, action: 'approve' }
        : { tool_call_id: tc.id, action: 'edit', args };
    }
    if (action === 'reject') {
      return { tool_call_id: tc.id, action: 'reject', reason: reasons[tc.id]?.trim() || 'Rejected by the user' };
    }
    return { tool_call_id: tc.id, action: 'approve' };
  };

  const disabled = !complete || !!busy;

  return (
    <View style={styles.card}>
      <Text style={styles.title} accessibilityRole="header">
        Your confirmation is needed
      </Text>
      {!!interrupt.message && <Text style={styles.msg}>{interrupt.message}</Text>}
      {!!note && <Text style={styles.note}>{note}</Text>}
      {interrupt.tool_calls.map((tc) => {
        const err = editError(tc);
        return (
          <View key={tc.id} style={styles.call}>
            <Text style={styles.name}>{tc.name}</Text>
            {choice[tc.id] !== 'edit' && <Text style={styles.args}>{pretty(tc.args)}</Text>}
            <View style={styles.row} accessibilityRole="radiogroup" accessibilityLabel={`Decision for ${tc.name}`}>
              {(['approve', 'edit', 'reject'] as const).map((a) => {
                const selected = choice[tc.id] === a;
                return (
                  <Pressable
                    key={a}
                    disabled={busy}
                    accessibilityRole="radio"
                    accessibilityLabel={`${LABEL[a]} ${tc.name}`}
                    accessibilityState={{ checked: selected, disabled: !!busy }}
                    onPress={() => setChoice((c) => ({ ...c, [tc.id]: a }))}
                    style={[styles.btn, selected && styles[a]]}
                  >
                    <Text>{LABEL[a]}</Text>
                  </Pressable>
                );
              })}
            </View>
            {choice[tc.id] === 'edit' && (
              <>
                <TextInput
                  style={[styles.input, styles.json, !!err && styles.invalid]}
                  accessibilityLabel={`Arguments for ${tc.name} (JSON)`}
                  value={editText(tc)}
                  onChangeText={(t) => setEdited((e) => ({ ...e, [tc.id]: t }))}
                  multiline
                  autoCapitalize="none"
                  autoCorrect={false}
                  spellCheck={false}
                  editable={!busy}
                />
                {!!err && <Text style={styles.error}>{err}</Text>}
              </>
            )}
            {choice[tc.id] === 'reject' && (
              <TextInput
                style={styles.input}
                accessibilityLabel={`Reason for rejecting ${tc.name}`}
                placeholder="Reason for rejecting (optional)"
                value={reasons[tc.id] ?? ''}
                onChangeText={(t) => setReasons((r) => ({ ...r, [tc.id]: t }))}
                editable={!busy}
              />
            )}
          </View>
        );
      })}
      <Pressable
        disabled={disabled}
        accessibilityRole="button"
        accessibilityState={{ disabled }}
        onPress={() => onSubmit(interrupt.tool_calls.map(decision))}
        style={[styles.submit, disabled && { opacity: 0.5 }]}
      >
        <Text style={styles.submitText}>Submit decision</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  card: { margin: 12, padding: 12, borderRadius: 12, borderWidth: 1, borderColor: '#f0b429', backgroundColor: '#fffbea' },
  title: { fontWeight: '700', marginBottom: 6 },
  msg: { marginBottom: 8, color: '#333' },
  note: { marginBottom: 8, color: '#8a5a00', fontStyle: 'italic' },
  call: { marginBottom: 10 },
  name: { fontFamily: 'monospace', fontWeight: '600' },
  args: { fontFamily: 'monospace', fontSize: 12, color: '#444', marginVertical: 4 },
  row: { flexDirection: 'row', gap: 8, marginTop: 4 },
  btn: { paddingHorizontal: 12, paddingVertical: 6, borderRadius: 8, borderWidth: 1, borderColor: '#ccc' },
  approve: { backgroundColor: '#d3f9d8', borderColor: '#2f9e44' },
  edit: { backgroundColor: '#e7f5ff', borderColor: '#1c7ed6' },
  reject: { backgroundColor: '#ffe3e3', borderColor: '#e03131' },
  input: { borderWidth: 1, borderColor: '#ddd', borderRadius: 8, padding: 8, marginTop: 8, backgroundColor: '#fff' },
  json: { fontFamily: 'monospace', fontSize: 12, minHeight: 80, textAlignVertical: 'top' },
  invalid: { borderColor: '#e03131' },
  error: { color: '#c92a2a', fontSize: 12, marginTop: 4 },
  submit: { backgroundColor: '#1f6feb', padding: 10, borderRadius: 8, alignItems: 'center' },
  submitText: { color: '#fff', fontWeight: '600' },
});
