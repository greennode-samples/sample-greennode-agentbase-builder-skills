// GreenNode IAM token of the BFF's service account (server only) — needed when the Runtime endpoint's Inbound Auth
// is "IAM Permissions" (BFF_RUNTIME_AUTH=iam). client_credentials, cached for 90% of expires_in, single-flight.
import { type BffConfig, IAM_TOKEN_URL } from './config';

let cache: { token: string; exp: number } | undefined;
let inflight: Promise<string> | undefined;

export async function iamToken(cfg: BffConfig, force = false): Promise<string> {
  if (!force && cache && cache.exp > Date.now()) return cache.token;
  if (inflight) return inflight;
  inflight = (async () => {
    const basic = btoa(`${cfg.iam.clientId}:${cfg.iam.clientSecret}`);
    const res = await fetch(IAM_TOKEN_URL, {
      method: 'POST',
      headers: { Authorization: `Basic ${basic}`, 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ grant_type: 'client_credentials' }),
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) throw new Error(`IAM token HTTP ${res.status}`); // 401: wrong client id / secret pair
    const data = (await res.json()) as { access_token: string; expires_in?: number };
    cache = { token: data.access_token, exp: Date.now() + (data.expires_in ?? 1800) * 900 };
    return data.access_token;
  })();
  try {
    return await inflight;
  } finally {
    inflight = undefined;
  }
}
