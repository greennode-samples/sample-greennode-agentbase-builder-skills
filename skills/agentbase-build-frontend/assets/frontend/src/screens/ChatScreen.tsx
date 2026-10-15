import Constants from 'expo-constants';
import React, { useRef } from 'react';
import {
  FlatList,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';

import type { Interrupt } from '../api/agentClient';
import { useConversation } from '../chat/ConversationProvider';
import { ApprovalCard } from '../components/ApprovalCard';
import { MessageBubble, type UiMessage } from '../components/MessageBubble';
import { env } from '../config/env';

// Insets without an extra dependency: the status bar height (edge-to-edge on Android, notch on iOS) at the top and
// room for the home indicator / gesture bar at the bottom. Exact insets: react-native-safe-area-context (SKILL.md).
const TOP_INSET = Constants.statusBarHeight ?? 0;
const BOTTOM_INSET = Platform.OS === 'web' ? 0 : 20;

// A new interrupt (new id) must get a fresh card: choices / edits / reasons of the previous one never carry over
const cardKey = (i: Interrupt) => i.id ?? i.tool_calls.map((t) => t.id).join(',');

export function ChatScreen() {
  const c = useConversation();
  const { state, busy } = c;
  const listRef = useRef<FlatList<UiMessage>>(null);
  const notice = !busy && !state.pending ? state.notice : null;

  return (
    <KeyboardAvoidingView
      style={styles.container}
      // Android (SDK 57) is edge-to-edge: the window is no longer resized for the keyboard ⇒ pad on both platforms
      behavior={Platform.OS === 'web' ? undefined : 'padding'}
    >
      <View style={[styles.header, { paddingTop: TOP_INSET + 12 }]}>
        <Text style={styles.title} numberOfLines={1}>
          __PROJECT_NAME__
        </Text>
        <View style={styles.headerActions}>
          <Pressable onPress={c.newConversation} accessibilityRole="button" accessibilityLabel="New conversation">
            <Text style={styles.link}>New</Text>
          </Pressable>
          {env.authMode === 'jwt' && (
            <Pressable onPress={c.signOut} accessibilityRole="button">
              <Text style={styles.link}>Sign out</Text>
            </Pressable>
          )}
        </View>
      </View>
      <FlatList
        ref={listRef}
        data={state.messages}
        keyExtractor={(m) => m.id}
        renderItem={({ item }) => <MessageBubble message={item} onFeedback={c.feedback} />}
        onContentSizeChange={() => listRef.current?.scrollToEnd({ animated: true })}
        contentContainerStyle={{ paddingVertical: 8 }}
        keyboardShouldPersistTaps="handled"
        ListFooterComponent={
          state.pending ? (
            <ApprovalCard
              key={cardKey(state.pending)}
              interrupt={state.pending}
              note={state.pendingNote}
              onSubmit={c.decide}
              busy={busy}
            />
          ) : null
        }
      />
      {notice && (
        <View style={styles.notice} accessibilityLiveRegion="polite">
          <Text style={styles.noticeText}>{notice.text}</Text>
          <Pressable
            onPress={notice.action === 'retry' ? c.retry : c.newConversation}
            accessibilityRole="button"
            style={styles.noticeBtn}
          >
            <Text style={styles.link}>{notice.action === 'retry' ? 'Retry' : 'Start new conversation'}</Text>
          </Pressable>
          <Pressable onPress={c.dismissNotice} accessibilityRole="button" accessibilityLabel="Dismiss">
            <Text style={styles.dismiss}>✕</Text>
          </Pressable>
        </View>
      )}
      <View style={[styles.composer, { paddingBottom: 8 + BOTTOM_INSET }]}>
        <TextInput
          style={styles.input}
          value={c.input}
          onChangeText={c.setInput}
          placeholder={state.pending ? 'Please confirm the request above…' : 'Type a message…'}
          accessibilityLabel="Message"
          multiline
          editable={!state.pending}
        />
        {busy ? (
          // Stop aborts this turn only — the session (and its memory) is kept
          <Pressable onPress={c.stop} style={[styles.send, styles.stop]} accessibilityRole="button">
            <Text style={styles.sendText}>Stop</Text>
          </Pressable>
        ) : (
          <Pressable
            onPress={c.send}
            disabled={!!state.pending || !c.input.trim()}
            accessibilityRole="button"
            accessibilityState={{ disabled: !!state.pending || !c.input.trim() }}
            style={[styles.send, (!!state.pending || !c.input.trim()) && { opacity: 0.5 }]}
          >
            <Text style={styles.sendText}>Send</Text>
          </Pressable>
        )}
      </View>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#fff' },
  header: {
    paddingHorizontal: 16,
    paddingBottom: 12,
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    borderBottomWidth: 1,
    borderBottomColor: '#eee',
  },
  headerActions: { flexDirection: 'row', gap: 16 },
  title: { fontSize: 18, fontWeight: '600', flexShrink: 1 },
  link: { color: '#1f6feb', fontSize: 15 },
  notice: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
    paddingHorizontal: 12,
    paddingVertical: 8,
    backgroundColor: '#fff4e6',
    borderTopWidth: 1,
    borderTopColor: '#ffd8a8',
  },
  noticeText: { flex: 1, color: '#7c4a03' },
  noticeBtn: { paddingVertical: 4 },
  dismiss: { color: '#7c4a03', fontSize: 16, paddingHorizontal: 4 },
  composer: { flexDirection: 'row', padding: 8, gap: 8, borderTopWidth: 1, borderTopColor: '#eee' },
  input: {
    flex: 1,
    minHeight: 40,
    maxHeight: 120,
    borderWidth: 1,
    borderColor: '#ddd',
    borderRadius: 10,
    paddingHorizontal: 10,
    paddingVertical: 8,
    fontSize: 15,
  },
  send: { backgroundColor: '#1f6feb', borderRadius: 10, paddingHorizontal: 16, justifyContent: 'center' },
  stop: { backgroundColor: '#e03131' },
  sendText: { color: '#fff', fontWeight: '600' },
});
