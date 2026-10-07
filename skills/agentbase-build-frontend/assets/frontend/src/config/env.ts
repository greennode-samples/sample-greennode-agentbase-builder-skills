// Config read from EXPO_PUBLIC_* (inlined at build time). Never put secrets in the frontend.
export const env = {
  agentUrl: (process.env.EXPO_PUBLIC_AGENT_URL ?? 'http://127.0.0.1:8080').replace(/\/$/, ''),
  authMode: (process.env.EXPO_PUBLIC_AUTH_MODE ?? 'jwt') as 'jwt' | 'none',
  authTokenHeader: process.env.EXPO_PUBLIC_AUTH_TOKEN_HEADER ?? 'Authorization',
  oidc: {
    issuer: process.env.EXPO_PUBLIC_OIDC_ISSUER ?? '',
    clientId: process.env.EXPO_PUBLIC_OIDC_CLIENT_ID ?? '',
    scopes: (process.env.EXPO_PUBLIC_OIDC_SCOPES ?? 'openid profile email offline_access').split(' '),
    audience: process.env.EXPO_PUBLIC_OIDC_AUDIENCE ?? '',
  },
  streaming: (process.env.EXPO_PUBLIC_STREAMING ?? 'true') === 'true',
};
