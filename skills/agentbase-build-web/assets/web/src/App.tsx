'use client';
// AuthProvider → ConversationProvider → sign-in screen (errors shown) | ChatScreen.
// The conversation lives above the auth gate, as in the Expo app.
import React from 'react';

import { AuthProvider, useAuth } from './auth/AuthProvider';
import { ConversationProvider } from './chat/ConversationProvider';
import { ChatScreen } from './screens/ChatScreen';

function Gate() {
  const auth = useAuth();
  if (auth.status === 'loading') return <p className="center muted">Loading…</p>;
  if (auth.status === 'signed_out') {
    return (
      <div className="center">
        <h1>__PROJECT_NAME__</h1>
        {!!auth.error && (
          <p className="error" role="alert">
            {auth.error}
          </p>
        )}
        <button type="button" className="primary" onClick={auth.signIn}>
          Sign in
        </button>
      </div>
    );
  }
  return <ChatScreen />;
}

export function App() {
  return (
    <AuthProvider>
      <ConversationProvider>
        <Gate />
      </ConversationProvider>
    </AuthProvider>
  );
}
