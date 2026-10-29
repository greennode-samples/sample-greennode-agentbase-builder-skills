# Web chat security — threat model and checklist

Complements `/agentbase-build` → `references/security.md` (the agent's own defenses). Here: what a browser adds.

## Threats and defenses in the template

| Threat | Defense |
|---|---|
| XSS steals tokens | No token in the browser: sealed httpOnly cookie, `/api/auth/session` returns no token, nothing in `localStorage` / `sessionStorage` |
| XSS through model output (prompt injection makes the agent emit `<script>`, `<img onerror>`, `javascript:` links) | Output rendered as React text nodes; `linkify` links `https://` only and stops at quotes / `<` / `>` / backticks; `MessageBubble` re-checks the URL; CSP blocks any script without the request's nonce |
| Clickjacking (approve button framed by another site) | `frame-ancestors 'none'` + `X-Frame-Options: DENY` |
| CSRF (another site makes the user's browser chat / approve / sign out) | `SameSite=Lax` cookie **and** Origin / `Sec-Fetch-Site` check **and** per-session `X-CSRF-Token` on every POST; no CORS headers |
| Login CSRF / code injection at the callback | `state` bound to a sealed, 10-minute cookie (constant-time compare), PKCE S256, ID token `nonce` |
| Cookie theft / planting | `Secure`, `__Host-` prefix (no `Domain`, a sibling subdomain cannot set it), sealed + authenticated (tampering ⇒ signed out), absolute expiry |
| Secrets in the bundle | Only `NEXT_PUBLIC_STREAMING` / `NEXT_PUBLIC_IDLE_TIMEOUT_S` reach the browser; IdP / IAM / session secrets and the agent URL are server-only `BFF_*` |
| Open redirect | Sign-out redirect = own path or the IdP's `end_session_endpoint` (https); callback always redirects to `/` |
| Exfiltration by injected content | `connect-src 'self'` (fetch / WebSocket only to this origin), `img-src 'self' data: blob:`, `form-action 'self'`, `Referrer-Policy: no-referrer` |
| Header / path injection towards the agent | The BFF builds upstream headers itself (no browser header or cookie forwarded), validates the session id regex and the body type / size |
| Token replay against other services | The user's access token goes only to the agent whose audience it carries; IAM token only with `BFF_RUNTIME_AUTH=iam` |
| Unsafe production config | `parseConfig` refuses `BFF_AUTH_MODE=none`, http `BFF_APP_ORIGIN` / `BFF_AGENT_URL`, a session secret < 32 chars, missing IdP / IAM settings |

## CSP

Pages (`src/web/src/proxy.ts` + `src/web/src/lib/csp.ts`, fresh nonce per request; Next.js adds it to its own
scripts and styles — pages are rendered per request for that):

```
default-src 'self'; script-src 'self' 'nonce-<n>' 'strict-dynamic'; style-src 'self' 'nonce-<n>';
img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none';
form-action 'self'; frame-ancestors 'none'; upgrade-insecure-requests
```

Development adds `'unsafe-eval'` (React dev tooling), `'unsafe-inline'` styles (error overlay) and `ws:` (HMR) —
never in production. `/api/*` responses get `default-src 'none'; frame-ancestors 'none'; sandbox` from
`next.config.ts`. Because styles need the nonce, components use CSS classes (`globals.css`), never `style={{…}}`.
Adding a third-party script, font or analytics endpoint means extending the policy explicitly — review why.

## Rendering model output

- Never `dangerouslySetInnerHTML`. Markdown rendering: only a renderer that outputs React elements without raw HTML
  (e.g. `react-markdown` without `rehype-raw`) and a link component that keeps the `https:`-only rule.
- Tool names / args and the interrupt message in the approval card are untrusted too (args come from the model):
  shown as text in `<pre>`; the user approves what is displayed.
- Links open in a new tab with `noopener noreferrer`: the agent-provided page cannot script this tab or learn its URL.

## Checklist before going live

- [ ] `BFF_APP_ORIGIN` is the https origin users type; redirect + post-logout URIs registered exactly
- [ ] `BFF_SESSION_SECRET`, `BFF_OIDC_CLIENT_SECRET`, `BFF_IAM_CLIENT_SECRET` set by the user in the host's secret store
- [ ] `NEXT_PUBLIC_*` contain no secret (`grep -r NEXT_PUBLIC_ .env*`)
- [ ] Response of `/` has the nonce CSP; `/api/invocations` without `X-CSRF-Token` ⇒ 403; from another origin ⇒ 403
- [ ] DevTools → Application: only `__Host-ab_session.*` cookies (HttpOnly, Secure), no tokens in storage
- [ ] Streaming arrives token by token through every proxy (no buffering / gzip on `text/event-stream`)
- [ ] Sign out → Sign in asks for credentials; a revoked user gets 401 → sign-in screen
- [ ] `npm audit --omit=dev` reviewed; Next.js kept on a patched release of the pinned major
