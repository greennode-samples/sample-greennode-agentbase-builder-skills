import * as Crypto from 'expo-crypto';
import React, { useCallback, useRef, useState } from 'react';
import {
  ActivityIndicator,
  FlatList,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';

import {
  AgentError,
  chatBody,
  type ChatResult,
  type Decision,
  type Interrupt,
  invoke,
  invokeStream,
  resumeBody,
  sendFeedback,
} from '../api/agentClient';
import { useAuth } from '../auth/AuthProvider';
import { ApprovalCard } from '../components/ApprovalCard';
import { MessageBubble, type UiMessage } from '../components/MessageBubble';
import { env } from '../config/env';

type Body = ReturnType<typeof chatBody>;

export function ChatScreen() {
  const { userId, getAccessToken, signOut } = useAuth();
  // 1 session = 1 conversation (short-term memory). "New" => new session.
  const [sessionId, setSessionId] = useState(() => Crypto.randomUUID());
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [pending, setPending] = useState<Interrupt | null>(null);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const listRef = useRef<FlatList<UiMessage>>(null);

  const patch = (id: string, fn: (m: UiMessage) => UiMessage) =>
    setMessages((ms) => ms.map((m) => (m.id === id ? fn(m) : m)));

  const finish = (replyId: string, r: ChatResult) => {
    if (r.status === 'interrupted') {
      setPending(r.interrupt);
      patch(replyId, (m) => ({ ...m, text: r.interrupt.message || 'Waiting for your confirmation…', pending: false }));
    } else {
      patch(replyId, (m) => ({
        ...m,
        text: r.response,
        traceId: r.trace_id,
        feedbackToken: r.feedback_token,
        pending: false,
      }));
    }
  };

  // Run one turn (chat or resume), streaming or not
  const run = useCallback(
    async (body: Body | ReturnType<typeof resumeBody>): Promise<boolean> => {
      setBusy(true);
      let ok = true;
      const replyId = Crypto.randomUUID();
      setMessages((ms) => [...ms, { id: replyId, role: 'assistant', text: '', pending: true, tools: [] }]);
      try {
        const ctx = { sessionId, userId, accessToken: await getAccessToken() };
        if (!env.streaming) {
          finish(replyId, await invoke(body, ctx));
          return true;
        }
        await invokeStream(body, ctx, (e) => {
          switch (e.event) {
            case 'token':
              patch(replyId, (m) => ({ ...m, text: m.text + e.data }));
              break;
            case 'reset': // self-eval requested a new answer
              patch(replyId, (m) => ({ ...m, text: '' }));
              break;
            case 'tool_start':
              patch(replyId, (m) => ({ ...m, tools: [...(m.tools ?? []), e.name] }));
              break;
            case 'interrupt':
            case 'done':
              finish(replyId, e as unknown as ChatResult);
              break;
            case 'error':
              ok = false;
              if (e.status === 401) void signOut();
              patch(replyId, (m) => ({ ...m, text: `⚠️ ${e.message}`, pending: false }));
              break;
          }
        });
      } catch (err) {
        ok = false;
        if (err instanceof AgentError && err.status === 401) await signOut();
        const msg = err instanceof Error ? err.message : String(err);
        patch(replyId, (m) => ({ ...m, text: `⚠️ ${msg}`, pending: false }));
      } finally {
        setBusy(false);
      }
      return ok;
    },
    [sessionId, userId, getAccessToken, signOut],
  );

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || busy || pending) return;
    setInput('');
    setMessages((ms) => [...ms, { id: Crypto.randomUUID(), role: 'user', text }]);
    await run(chatBody(text));
  }, [input, busy, pending, run]);

  const decide = useCallback(
    async (decisions: Decision[]) => {
      const current = pending;
      setPending(null);
      const ok = await run(resumeBody(decisions, current?.id));
      if (!ok) setPending(current); // resume failed (network/5xx) ⇒ keep the approval card, backend is still waiting
    },
    [run, pending],
  );

  const onFeedback = useCallback(
    async (m: UiMessage, score: -1 | 1) => {
      if (!m.traceId || !m.feedbackToken) return;
      patch(m.id, (x) => ({ ...x, feedback: score }));
      const ctx = { sessionId, userId, accessToken: await getAccessToken() };
      sendFeedback(m.traceId, m.feedbackToken, score, ctx).catch(() => undefined);
    },
    [sessionId, userId, getAccessToken],
  );

  const newSession = () => {
    setSessionId(Crypto.randomUUID());
    setMessages([]);
    setPending(null);
  };

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <View style={styles.header}>
        <Text style={styles.title}>__PROJECT_NAME__</Text>
        <Pressable onPress={newSession}>
          <Text style={styles.link}>New</Text>
        </Pressable>
      </View>
      <FlatList
        ref={listRef}
        data={messages}
        keyExtractor={(m) => m.id}
        renderItem={({ item }) => <MessageBubble message={item} onFeedback={onFeedback} />}
        onContentSizeChange={() => listRef.current?.scrollToEnd({ animated: true })}
        contentContainerStyle={{ paddingVertical: 8 }}
        ListFooterComponent={
          pending ? <ApprovalCard interrupt={pending} onSubmit={decide} busy={busy} /> : null
        }
      />
      <View style={styles.composer}>
        <TextInput
          style={styles.input}
          value={input}
          onChangeText={setInput}
          placeholder={pending ? 'Please confirm the request above…' : 'Type a message…'}
          multiline
          editable={!busy && !pending}
        />
        <Pressable onPress={send} disabled={busy || !!pending} style={styles.send}>
          {busy ? <ActivityIndicator color="#fff" /> : <Text style={styles.sendText}>Send</Text>}
        </Pressable>
      </View>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#fff' },
  header: {
    paddingTop: 56,
    paddingHorizontal: 16,
    paddingBottom: 12,
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    borderBottomWidth: 1,
    borderBottomColor: '#eee',
  },
  title: { fontSize: 18, fontWeight: '600' },
  link: { color: '#1f6feb', fontSize: 15 },
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
  sendText: { color: '#fff', fontWeight: '600' },
});
