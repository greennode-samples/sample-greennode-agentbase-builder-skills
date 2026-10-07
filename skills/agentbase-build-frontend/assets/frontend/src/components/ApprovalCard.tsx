// Human-in-the-loop approval card: shows pending tool calls, Approve / Reject each one.
import React, { useState } from 'react';
import { Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import type { Decision, Interrupt } from '../api/agentClient';

type Props = { interrupt: Interrupt; onSubmit: (decisions: Decision[]) => void; busy?: boolean };

export function ApprovalCard({ interrupt, onSubmit, busy }: Props) {
  const [choice, setChoice] = useState<Record<string, 'approve' | 'reject'>>({});
  const [reason, setReason] = useState('');
  const complete = interrupt.tool_calls.every((tc) => choice[tc.id]);

  const submit = () =>
    onSubmit(
      interrupt.tool_calls.map((tc) => ({
        tool_call_id: tc.id,
        action: choice[tc.id] ?? 'reject',
        reason: choice[tc.id] === 'reject' ? reason || 'Rejected by the user' : undefined,
      })),
    );

  return (
    <View style={styles.card}>
      <Text style={styles.title}>Your confirmation is needed</Text>
      {!!interrupt.message && <Text style={styles.msg}>{interrupt.message}</Text>}
      {interrupt.tool_calls.map((tc) => (
        <View key={tc.id} style={styles.call}>
          <Text style={styles.name}>{tc.name}</Text>
          <Text style={styles.args}>{JSON.stringify(tc.args, null, 2)}</Text>
          <View style={styles.row}>
            {(['approve', 'reject'] as const).map((a) => (
              <Pressable
                key={a}
                onPress={() => setChoice((c) => ({ ...c, [tc.id]: a }))}
                style={[styles.btn, choice[tc.id] === a && (a === 'approve' ? styles.ok : styles.no)]}
              >
                <Text>{a === 'approve' ? 'Approve' : 'Reject'}</Text>
              </Pressable>
            ))}
          </View>
        </View>
      ))}
      {Object.values(choice).includes('reject') && (
        <TextInput style={styles.input} placeholder="Reason for rejecting" value={reason} onChangeText={setReason} />
      )}
      <Pressable disabled={!complete || busy} onPress={submit} style={[styles.submit, (!complete || busy) && { opacity: 0.5 }]}>
        <Text style={styles.submitText}>Submit decision</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  card: { margin: 12, padding: 12, borderRadius: 12, borderWidth: 1, borderColor: '#f0b429', backgroundColor: '#fffbea' },
  title: { fontWeight: '700', marginBottom: 6 },
  msg: { marginBottom: 8, color: '#333' },
  call: { marginBottom: 10 },
  name: { fontFamily: 'monospace', fontWeight: '600' },
  args: { fontFamily: 'monospace', fontSize: 12, color: '#444', marginVertical: 4 },
  row: { flexDirection: 'row', gap: 8 },
  btn: { paddingHorizontal: 12, paddingVertical: 6, borderRadius: 8, borderWidth: 1, borderColor: '#ccc' },
  ok: { backgroundColor: '#d3f9d8', borderColor: '#2f9e44' },
  no: { backgroundColor: '#ffe3e3', borderColor: '#e03131' },
  input: { borderWidth: 1, borderColor: '#ddd', borderRadius: 8, padding: 8, marginBottom: 8 },
  submit: { backgroundColor: '#1f6feb', padding: 10, borderRadius: 8, alignItems: 'center' },
  submitText: { color: '#fff', fontWeight: '600' },
});
