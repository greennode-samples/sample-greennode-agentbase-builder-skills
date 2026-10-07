import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

export type UiMessage = {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  traceId?: string | null;
  feedbackToken?: string | null;
  tools?: string[];
  feedback?: -1 | 1;
  pending?: boolean;
};

type Props = { message: UiMessage; onFeedback?: (m: UiMessage, score: -1 | 1) => void };

export function MessageBubble({ message, onFeedback }: Props) {
  const mine = message.role === 'user';
  return (
    <View style={[styles.row, mine ? styles.right : styles.left]}>
      <View style={[styles.bubble, mine ? styles.mine : styles.theirs]}>
        {!!message.tools?.length && (
          <Text style={styles.tools}>🔧 {message.tools.join(', ')}</Text>
        )}
        <Text style={mine ? styles.mineText : styles.text}>
          {message.text || (message.pending ? '…' : '')}
        </Text>
        {!mine && message.traceId && !message.pending && (
          <View style={styles.actions}>
            {([1, -1] as const).map((s) => (
              <Pressable
                key={s}
                accessibilityLabel={s === 1 ? 'Helpful' : 'Not helpful'}
                onPress={() => onFeedback?.(message, s)}
                style={[styles.btn, message.feedback === s && styles.btnOn]}
              >
                <Text>{s === 1 ? '👍' : '👎'}</Text>
              </Pressable>
            ))}
          </View>
        )}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  row: { marginVertical: 4, paddingHorizontal: 12, flexDirection: 'row' },
  left: { justifyContent: 'flex-start' },
  right: { justifyContent: 'flex-end' },
  bubble: { maxWidth: '85%', borderRadius: 14, padding: 10 },
  mine: { backgroundColor: '#1f6feb' },
  theirs: { backgroundColor: '#f0f2f5' },
  mineText: { color: '#fff', fontSize: 15 },
  text: { color: '#111', fontSize: 15 },
  tools: { fontSize: 12, color: '#666', marginBottom: 4 },
  actions: { flexDirection: 'row', gap: 8, marginTop: 6 },
  btn: { paddingHorizontal: 6, paddingVertical: 2, borderRadius: 8, opacity: 0.6 },
  btnOn: { opacity: 1, backgroundColor: '#dde3ea' },
});
