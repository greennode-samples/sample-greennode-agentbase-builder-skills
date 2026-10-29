// Browser configuration: NEXT_PUBLIC_* only (inlined into the public JavaScript bundle at BUILD time — keep the literal
// `process.env.NEXT_PUBLIC_X` accesses). NEVER a secret, token or the agent URL: the browser only talks to this app's
// own /api routes (the BFF). Server-side settings (BFF_*) are read in src/server/config.ts.
const num = (v: string | undefined, fallback: number) => (v && Number.isFinite(Number(v)) ? Number(v) : fallback);

export const env = {
  streaming: (process.env.NEXT_PUBLIC_STREAMING ?? 'true') === 'true',
  // Give up when the agent sends NOTHING for this long (re-armed on every streamed chunk). Default = backend
  // REQUEST_TIMEOUT_S (180) + margin, so the backend's own 504 normally arrives first.
  idleTimeoutMs: num(process.env.NEXT_PUBLIC_IDLE_TIMEOUT_S, 200) * 1000,
  dev: process.env.NODE_ENV !== 'production',
};
