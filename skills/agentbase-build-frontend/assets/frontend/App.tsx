import { StatusBar } from 'expo-status-bar';
import React, { useState } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';

import { AuthProvider, useAuth } from './src/auth/AuthProvider';
import { ConversationProvider, useConversation } from './src/chat/ConversationProvider';
import { ChatScreen } from './src/screens/ChatScreen';

function SignInScreen() {
  const { signIn } = useAuth();
  const { state } = useConversation();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const onPress = async () => {
    setError(null);
    setBusy(true);
    try {
      await signIn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e)); // e.g. invalid redirect_uri, IdP unreachable
    } finally {
      setBusy(false);
    }
  };
  return (
    <View style={styles.center}>
      <Text style={styles.title}>__PROJECT_NAME__</Text>
      {state.messages.length > 0 && <Text style={styles.hint}>Sign in again to continue your conversation.</Text>}
      <Pressable style={[styles.btn, busy && { opacity: 0.6 }]} onPress={onPress} disabled={busy} accessibilityRole="button">
        {busy ? <ActivityIndicator color="#fff" /> : <Text style={styles.btnText}>Sign in</Text>}
      </Pressable>
      {!!error && (
        <Text style={styles.error} accessibilityLiveRegion="polite">
          {error}
        </Text>
      )}
    </View>
  );
}

function Root() {
  const { ready, signedIn } = useAuth();
  if (!ready) return <ActivityIndicator style={{ flex: 1 }} />;
  return signedIn ? <ChatScreen /> : <SignInScreen />;
}

export default function App() {
  return (
    <AuthProvider>
      {/* Above the auth gate: an expired session + sign-in again keeps the conversation */}
      <ConversationProvider>
        <StatusBar style="dark" />
        <Root />
      </ConversationProvider>
    </AuthProvider>
  );
}

const styles = StyleSheet.create({
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 16, padding: 24, backgroundColor: '#fff' },
  title: { fontSize: 22, fontWeight: '600' },
  hint: { color: '#555' },
  btn: { backgroundColor: '#1f6feb', paddingHorizontal: 24, paddingVertical: 12, borderRadius: 10, minWidth: 120, alignItems: 'center' },
  btnText: { color: '#fff', fontWeight: '600' },
  error: { color: '#c92a2a', textAlign: 'center' },
});
