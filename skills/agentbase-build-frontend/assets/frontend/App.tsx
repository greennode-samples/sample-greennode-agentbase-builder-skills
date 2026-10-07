import { StatusBar } from 'expo-status-bar';
import React from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';

import { AuthProvider, useAuth } from './src/auth/AuthProvider';
import { ChatScreen } from './src/screens/ChatScreen';

function Root() {
  const { ready, signedIn, signIn } = useAuth();
  if (!ready) return <ActivityIndicator style={{ flex: 1 }} />;
  if (!signedIn) {
    return (
      <View style={styles.center}>
        <Text style={styles.title}>__PROJECT_NAME__</Text>
        <Pressable style={styles.btn} onPress={() => signIn().catch(console.warn)}>
          <Text style={styles.btnText}>Sign in</Text>
        </Pressable>
      </View>
    );
  }
  return <ChatScreen />;
}

export default function App() {
  return (
    <AuthProvider>
      <StatusBar style="dark" />
      <Root />
    </AuthProvider>
  );
}

const styles = StyleSheet.create({
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 16 },
  title: { fontSize: 22, fontWeight: '600' },
  btn: { backgroundColor: '#1f6feb', paddingHorizontal: 24, paddingVertical: 12, borderRadius: 10 },
  btnText: { color: '#fff', fontWeight: '600' },
});
