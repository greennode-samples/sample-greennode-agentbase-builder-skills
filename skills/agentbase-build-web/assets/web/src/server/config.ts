// BFF configuration — SERVER-ONLY environment variables (no NEXT_PUBLIC_ prefix: Next.js never inlines them into the
// browser bundle). Secrets live here: OIDC client secret, IAM service account, session secret.
// Read lazily (first request), not at import: `next build` must not need production secrets.

export const IAM_TOKEN_URL = 'https://iam.api.vngcloud.vn/accounts-api/v2/auth/token';

export type BffConfig = {
  /** AgentBase Runtime endpoint (or local backend) — the BFF calls `<agentUrl>/invocations` */
  agentUrl: string;
  /** jwt: OIDC sign-in, the user's access token goes to the agent · none: local backend with AUTH_MODE=none */
  authMode: 'jwt' | 'none';
  /** How the Runtime endpoint authenticates: jwt (user token in Authorization) · iam (IAM token in Authorization) */
  runtimeAuth: 'jwt' | 'iam';
  iam: { clientId: string; clientSecret: string };
  oidc: { issuer: string; clientId: string; clientSecret: string; scopes: string; audience: string };
  /** Public origin of this web app (redirect URI, CSRF Origin check), e.g. https://chat.example.com */
  appOrigin: string;
  /** https origin ⇒ Secure + __Host- cookies */
  secure: boolean;
  sessionSecret: string;
  sessionMaxAgeS: number;
};

export class ConfigError extends Error {}

type Env = Record<string, string | undefined>;

const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]']);
const isHttps = (u: string) => /^https:\/\//i.test(u);

function hostname(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return '';
  }
}

function origin(value: string): string {
  try {
    return new URL(value).origin;
  } catch {
    throw new ConfigError(`BFF_APP_ORIGIN is not a URL: ${value}`);
  }
}

/** Pure: env → config; `production` ⇒ unsafe values are refused (ConfigError lists every problem). */
export function parseConfig(env: Env, production: boolean, devSecret = ''): BffConfig {
  const num = (v: string | undefined, d: number) => (v && Number.isFinite(Number(v)) ? Number(v) : d);
  const appOrigin = origin(env.BFF_APP_ORIGIN || 'http://localhost:3000');
  const cfg: BffConfig = {
    agentUrl: (env.BFF_AGENT_URL || 'http://127.0.0.1:8080').replace(/\/+$/, ''),
    authMode: env.BFF_AUTH_MODE === 'none' ? 'none' : 'jwt',
    runtimeAuth: env.BFF_RUNTIME_AUTH === 'iam' ? 'iam' : 'jwt',
    iam: { clientId: env.BFF_IAM_CLIENT_ID ?? '', clientSecret: env.BFF_IAM_CLIENT_SECRET ?? '' },
    oidc: {
      issuer: (env.BFF_OIDC_ISSUER ?? '').replace(/\/+$/, ''),
      clientId: env.BFF_OIDC_CLIENT_ID ?? '',
      clientSecret: env.BFF_OIDC_CLIENT_SECRET ?? '',
      scopes: env.BFF_OIDC_SCOPES || 'openid profile email offline_access',
      audience: env.BFF_OIDC_AUDIENCE ?? '',
    },
    appOrigin,
    secure: isHttps(appOrigin),
    sessionSecret: env.BFF_SESSION_SECRET || devSecret,
    sessionMaxAgeS: num(env.BFF_SESSION_MAX_AGE_S, 8 * 3600),
  };
  const problems: string[] = [];
  if (cfg.runtimeAuth === 'iam' && (!cfg.iam.clientId || !cfg.iam.clientSecret)) {
    problems.push('BFF_RUNTIME_AUTH=iam needs BFF_IAM_CLIENT_ID and BFF_IAM_CLIENT_SECRET');
  }
  if (cfg.authMode === 'jwt' && (!cfg.oidc.issuer || !cfg.oidc.clientId)) {
    problems.push('BFF_OIDC_ISSUER / BFF_OIDC_CLIENT_ID are empty (BFF_AUTH_MODE=jwt)');
  }
  if (cfg.authMode === 'jwt' && cfg.oidc.issuer && !isHttps(cfg.oidc.issuer) && production) {
    problems.push('BFF_OIDC_ISSUER must be https://');
  }
  if (production) {
    if (cfg.authMode === 'none') problems.push('BFF_AUTH_MODE=none is for local development only');
    if (!isHttps(cfg.appOrigin)) problems.push('BFF_APP_ORIGIN must be https:// (Secure cookies)');
    if (!isHttps(cfg.agentUrl) && !LOCAL_HOSTS.has(hostname(cfg.agentUrl))) {
      problems.push('BFF_AGENT_URL must be https:// (user tokens travel on it)');
    }
    if ((env.BFF_SESSION_SECRET ?? '').length < 32) {
      problems.push('BFF_SESSION_SECRET must be set to >= 32 random characters (openssl rand -base64 48)');
    }
  }
  if (problems.length) throw new ConfigError(`[bff config] ${problems.join('; ')}`);
  return cfg;
}

let cached: BffConfig | undefined;

/** The validated configuration (throws ConfigError — the route answers 500 and logs the reason). */
export function bffConfig(): BffConfig {
  if (cached) return cached;
  const production = process.env.NODE_ENV === 'production';
  // Development without BFF_SESSION_SECRET: a random secret per server process (sessions end on restart)
  const g = globalThis as { __abDevSessionSecret?: string };
  if (!production && !process.env.BFF_SESSION_SECRET && !g.__abDevSessionSecret) {
    g.__abDevSessionSecret = Array.from(crypto.getRandomValues(new Uint8Array(32)), (b) => b.toString(16)).join('');
    console.warn('[bff] BFF_SESSION_SECRET not set — using a random development secret (sessions end on restart)');
  }
  // Literal process.env.X reads (one per variable) keep the variables discoverable; none of them is NEXT_PUBLIC_
  cached = parseConfig(
    {
      BFF_AGENT_URL: process.env.BFF_AGENT_URL,
      BFF_AUTH_MODE: process.env.BFF_AUTH_MODE,
      BFF_RUNTIME_AUTH: process.env.BFF_RUNTIME_AUTH,
      BFF_IAM_CLIENT_ID: process.env.BFF_IAM_CLIENT_ID,
      BFF_IAM_CLIENT_SECRET: process.env.BFF_IAM_CLIENT_SECRET,
      BFF_OIDC_ISSUER: process.env.BFF_OIDC_ISSUER,
      BFF_OIDC_CLIENT_ID: process.env.BFF_OIDC_CLIENT_ID,
      BFF_OIDC_CLIENT_SECRET: process.env.BFF_OIDC_CLIENT_SECRET,
      BFF_OIDC_SCOPES: process.env.BFF_OIDC_SCOPES,
      BFF_OIDC_AUDIENCE: process.env.BFF_OIDC_AUDIENCE,
      BFF_APP_ORIGIN: process.env.BFF_APP_ORIGIN,
      BFF_SESSION_SECRET: process.env.BFF_SESSION_SECRET,
      BFF_SESSION_MAX_AGE_S: process.env.BFF_SESSION_MAX_AGE_S,
    },
    production,
    g.__abDevSessionSecret,
  );
  return cached;
}
