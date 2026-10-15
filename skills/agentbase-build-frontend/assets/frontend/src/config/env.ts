// Config read from EXPO_PUBLIC_* (inlined at BUILD time — keep the literal `process.env.EXPO_PUBLIC_X` accesses).
// Never put secrets in the frontend.
// Release builds read them from the build environment: src/frontend/.env is git-ignored, so EAS Build does NOT
// get it — define them with `eas env:create` or in eas.json `build.<profile>.env` (see SKILL.md).
const num = (v: string | undefined, fallback: number) => (v && Number.isFinite(Number(v)) ? Number(v) : fallback);

export const env = {
  agentUrl: (process.env.EXPO_PUBLIC_AGENT_URL || 'http://127.0.0.1:8080').replace(/\/+$/, ''),
  authMode: (process.env.EXPO_PUBLIC_AUTH_MODE === 'none' ? 'none' : 'jwt') as 'jwt' | 'none',
  // Header the APP sends the user JWT in: `Authorization` in both Direct and BFF mode (in BFF mode the BFF moves it
  // to X-GreenNode-AgentBase-Custom-User-Token itself) — NOT necessarily the backend's AUTH_TOKEN_HEADER.
  authTokenHeader: process.env.EXPO_PUBLIC_AUTH_TOKEN_HEADER || 'Authorization',
  oidc: {
    issuer: (process.env.EXPO_PUBLIC_OIDC_ISSUER ?? '').replace(/\/+$/, ''),
    clientId: process.env.EXPO_PUBLIC_OIDC_CLIENT_ID ?? '',
    scopes: (process.env.EXPO_PUBLIC_OIDC_SCOPES || 'openid profile email offline_access').split(/\s+/).filter(Boolean),
    audience: process.env.EXPO_PUBLIC_OIDC_AUDIENCE ?? '',
    // iOS: don't share the IdP cookie with Safari (no SSO across apps; every sign-in asks for credentials)
    ephemeralSession: process.env.EXPO_PUBLIC_OIDC_EPHEMERAL_SESSION === 'true',
  },
  streaming: (process.env.EXPO_PUBLIC_STREAMING ?? 'true') === 'true',
  // Give up when the agent sends NOTHING for this long (re-armed on every streamed chunk). Default = backend
  // REQUEST_TIMEOUT_S (180) + margin, so the backend's own 504 normally arrives first.
  idleTimeoutMs: num(process.env.EXPO_PUBLIC_IDLE_TIMEOUT_S, 200) * 1000,
};

// A release build must never fall back to a developer default (http://127.0.0.1, no IdP) silently: fail at startup
// with the reason instead of shipping an app that cannot reach its backend.
if (!__DEV__) {
  const problems: string[] = [];
  if (!process.env.EXPO_PUBLIC_AGENT_URL) problems.push('EXPO_PUBLIC_AGENT_URL is not set');
  else if (!/^https:\/\//i.test(env.agentUrl)) problems.push('EXPO_PUBLIC_AGENT_URL must be an https:// URL');
  if (env.authMode === 'jwt' && (!env.oidc.issuer || !env.oidc.clientId)) {
    problems.push('EXPO_PUBLIC_OIDC_ISSUER / EXPO_PUBLIC_OIDC_CLIENT_ID are empty (AUTH_MODE=jwt)');
  }
  if (problems.length) {
    throw new Error(
      `[config] ${problems.join('; ')}. Set them in the build environment (EAS: \`eas env:create\` or eas.json ` +
        '"env" — src/frontend/.env is git-ignored and not uploaded).',
    );
  }
}
