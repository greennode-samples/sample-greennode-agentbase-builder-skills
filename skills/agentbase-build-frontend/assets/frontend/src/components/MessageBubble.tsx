import * as WebBrowser from 'expo-web-browser';
import React from 'react';
import { Linking, Pressable, StyleSheet, Text, View } from 'react-native';

import type { UiMessage } from '../chat/chatState';
import { linkify } from './linkify';

export type { UiMessage } from '../chat/chatState';

type Props = { message: UiMessage; onFeedback?: (m: UiMessage, score: -1 | 1) => void };

// In-app browser (SFSafariViewController / Custom Tabs; new tab on web): the user comes straight back to the chat,
// e.g. after the AUTHORIZATION_REQUIRED consent page from /agentbase-build-identity
const openLink = (url: string) =>
  WebBrowser.openBrowserAsync(url).catch(() => Linking.openURL(url).catch(() => undefined));

function Linkified({ text, style }: { text: string; style: object }) {
  return (
    <Text style={style}>
      {linkify(text).map((p, i) =>
        p.url ? (
          <Text key={i} style={styles.link} accessibilityRole="link" onPress={() => void openLink(p.url!)}>
            {p.text}
          </Text>
        ) : (
          p.text
        ),
      )}
    </Text>
  );
}

export function MessageBubble({ message, onFeedback }: Props) {
  const mine = message.role === 'user';
  // feedback needs BOTH ids from the response — the backend answers 403 without the token
  const canRate = !mine && !message.pending && !!message.traceId && !!message.feedbackToken;
  const showStatus = !mine && !!message.pending && !!message.status;
  return (
    <View style={[styles.row, mine ? styles.right : styles.left]}>
      <View style={[styles.bubble, mine ? styles.mine : styles.theirs]}>
        {!!message.tools?.length && <Text style={styles.tools}>🔧 {message.tools.join(', ')}</Text>}
        {(!!message.text || !showStatus) && (
          <Linkified
            style={mine ? styles.mineText : styles.text}
            text={message.text || (message.pending ? '…' : '')}
          />
        )}
        {showStatus && (
          // progress line ("Thinking…", "Searching the knowledge base…") until answer text arrives
          <Text style={styles.status} accessibilityLiveRegion="polite" numberOfLines={2}>
            {message.status}
          </Text>
        )}
        {canRate && (
          <View style={styles.actions}>
            {([1, -1] as const).map((s) => (
              <Pressable
                key={s}
                accessibilityRole="button"
                accessibilityLabel={s === 1 ? 'Helpful' : 'Not helpful'}
                accessibilityState={{ selected: message.feedback === s }}
                onPress={() => onFeedback?.(message, s)}
                style={[styles.btn, message.feedback === s && styles.btnOn]}
              >
                <Text>{s === 1 ? '👍' : '👎'}</Text>
              </Pressable>
            ))}
            {!!message.feedbackError && <Text style={styles.error}>{message.feedbackError}</Text>}
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
  status: { fontSize: 13, color: '#666', fontStyle: 'italic' },
  link: { color: '#0b5ed7', textDecorationLine: 'underline' },
  actions: { flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 6 },
  btn: { paddingHorizontal: 6, paddingVertical: 2, borderRadius: 8, opacity: 0.6 },
  btnOn: { opacity: 1, backgroundColor: '#dde3ea' },
  error: { fontSize: 12, color: '#c92a2a' },
});
