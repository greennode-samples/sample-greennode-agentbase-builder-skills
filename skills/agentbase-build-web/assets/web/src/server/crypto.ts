// Web Crypto helpers of the BFF (server only): random tokens, PKCE, sealed (encrypted + authenticated) cookie values.
// Pure — no Next.js imports — so it is unit-tested in plain Node (tests/session.test.ts).
const enc = new TextEncoder();
const dec = new TextDecoder();

export function b64urlEncode(bytes: Uint8Array): string {
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function b64urlDecode(text: string): Uint8Array<ArrayBuffer> {
  const b64 = text.replace(/-/g, '+').replace(/_/g, '/');
  const bin = atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Unpredictable token (state, nonce, PKCE verifier, CSRF token): 32 random bytes, base64url. */
export const randomToken = (bytes = 32) => b64urlEncode(crypto.getRandomValues(new Uint8Array(bytes)));

/** RFC 7636 S256 code challenge of a PKCE verifier. */
export async function pkceChallenge(verifier: string): Promise<string> {
  return b64urlEncode(new Uint8Array(await crypto.subtle.digest('SHA-256', enc.encode(verifier))));
}

/** Constant-time string comparison (CSRF token, OIDC state). */
export function timingSafeEqual(a: string, b: string): boolean {
  const x = enc.encode(a);
  const y = enc.encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

const keys = new Map<string, Promise<CryptoKey>>();

/** AES-256-GCM key derived with HKDF from the secret; `purpose` separates keys (session vs OIDC transaction). */
function key(secret: string, purpose: string): Promise<CryptoKey> {
  const id = `${purpose}\u0000${secret}`;
  let k = keys.get(id);
  if (!k) {
    k = crypto.subtle
      .importKey('raw', enc.encode(secret), 'HKDF', false, ['deriveKey'])
      .then((base) =>
        crypto.subtle.deriveKey(
          { name: 'HKDF', hash: 'SHA-256', salt: enc.encode('agentbase-web'), info: enc.encode(purpose) },
          base,
          { name: 'AES-GCM', length: 256 },
          false,
          ['encrypt', 'decrypt'],
        ),
      );
    keys.set(id, k);
  }
  return k;
}

/**
 * Encrypts + authenticates `value` (JSON) with an expiry: `v1.<iv>.<ciphertext>`. `purpose` is also the AAD, so a
 * value sealed for one cookie cannot be replayed as another.
 */
export async function seal(value: unknown, secret: string, purpose: string, maxAgeS: number): Promise<string> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const plain = enc.encode(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + maxAgeS, value }));
  const ct = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv, additionalData: enc.encode(purpose) },
    await key(secret, purpose),
    plain,
  );
  return `v1.${b64urlEncode(iv)}.${b64urlEncode(new Uint8Array(ct))}`;
}

/** The sealed value, or null when it is malformed, tampered with, sealed with another secret/purpose, or expired. */
export async function unseal<T>(sealed: string, secret: string, purpose: string): Promise<T | null> {
  try {
    const [v, iv, ct] = sealed.split('.');
    if (v !== 'v1' || !iv || !ct) return null;
    const plain = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: b64urlDecode(iv), additionalData: enc.encode(purpose) },
      await key(secret, purpose),
      b64urlDecode(ct),
    );
    const { exp, value } = JSON.parse(dec.decode(plain)) as { exp: number; value: T };
    return typeof exp === 'number' && exp > Date.now() / 1000 ? value : null;
  } catch {
    return null;
  }
}
