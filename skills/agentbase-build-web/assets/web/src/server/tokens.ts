// Keeps the user's access token fresh (server only): refreshed when it expires within 30 s, or forced after the
// agent answered 401 (token revoked / rotated early). The browser never takes part.
import type { BffConfig } from './config';
import { OidcError, refreshTokens, type TokenSet } from './oidc';
import type { Session } from './session';

const SKEW_S = 30;

/** Session fields from a token response (a refresh may omit the refresh / ID token: keep the previous ones). */
export function withTokens(s: Session, t: TokenSet): Session {
  return {
    ...s,
    accessToken: t.access_token,
    accessTokenExp: Math.floor(Date.now() / 1000) + (t.expires_in ?? 300),
    refreshToken: t.refresh_token ?? s.refreshToken,
    idToken: t.id_token ?? s.idToken,
  };
}

export async function freshSession(
  cfg: BffConfig,
  s: Session,
  force = false,
): Promise<{ session: Session; changed: boolean }> {
  const valid = !!s.accessToken && (s.accessTokenExp ?? 0) > Date.now() / 1000 + SKEW_S;
  if (valid && !force) return { session: s, changed: false };
  if (!s.refreshToken) throw new OidcError('invalid_grant', 'access token expired and no refresh token');
  return { session: withTokens(s, await refreshTokens(cfg, s.refreshToken)), changed: true };
}
