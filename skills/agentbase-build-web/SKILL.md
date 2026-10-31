---
name: agentbase-build-web
description: "Use when the user wants a website, web page or browser-based chat UI (Next.js) for their agent, e.g. employees chatting with it after signing in with Google or company SSO, or to update the web template to a newer version of these skills: load it before coding. Next.js App Router in src/web: BFF route handlers (OIDC PKCE, encrypted httpOnly cookie, CSRF), SSE streaming, approval cards, feedback, strict CSP, setup_web.sh --update. Trigger: website, web page, web app, web UI, web chat UI, Next.js, browser chat, SSO login, giao diện web, web chat. DO NOT use for the mobile app (/agentbase-build-frontend) or the backend."
---

# Next.js Web Chat (BFF)

Only create when the Agent Spec includes a **web** UI (mobile: `/agentbase-build-frontend`). Fixed location: `src/web`.
Same contract and chat behavior as the Expo app; the difference is **where the tokens live**: on the server.

## When to use

- A browser chat for the agent (internal tool, customer portal), with the user's own sign-in (OIDC).
- The Runtime endpoint requires IAM (pattern **B. BFF** of `/agentbase-build-auth`): this app *is* the BFF.

## Step 1 — Create the app

```bash
bash <skill-dir>/scripts/setup_web.sh <project-name> <project>/src/web [--no-install] [--force]
bash <skill-dir>/scripts/setup_web.sh <project-name> <project>/src/web --update     # a newer version of these skills
```

Tested with **create-next-app 16.4.3** (next 16.4.3, react 19.3.0, TypeScript 5) and **vitest 4.1.12** — pinned in the
script (override: `CREATE_NEXT_APP_VERSION=<x.y.z>`, `VITEST_VERSION=<x.y.z>`; another version is untested). Needs
network and **Node ≥ 20.9** (checked).

What it does: `CI=1 npx create-next-app@16.4.3 <dir> --ts --app --src-dir --empty --no-tailwind --no-eslint
--disable-git --no-agents-md --skip-install … --yes` (every option given, stdin closed ⇒ no prompt; no nested git
repo) → overlays `assets/web/` (replaces Next's page, layout and `next.config.ts`) → `__PROJECT_NAME__` replaced →
`.env.example` copied to `.env.local` and un-ignored (`!.env.example`) → `package.json` gets `test` / `typecheck`
scripts → `npm install` + `vitest`. Existing app: only **missing** template files are added (`--force` overwrites);
a failed run leaves `.agentbase-setup-pending` and the next run re-applies the template.

**Updating the template files** (BFF security fixes, new components) — `upgrade.py` does not touch `src/web`; `setup_web.sh … --update` does it safely: setup records the SHA-256 of every template file it wrote in `src/web/.agentbase-web.json` (**commit it**); `--update` then adds new files, **replaces the ones you never edited**, and for a file you edited keeps yours and writes the new version to `<file>.agentbase-new` — merge it, delete it, run again (exit 1 while something is left to merge). Dropped template files are listed, never deleted; `package.json`, the lock and your `.env.local` are never touched. An app from before the manifest existed has none: every file that differs is treated as yours. Run `make web-test` afterwards. Test: `.github/scripts/test_web_update.py`.

## Language and accessibility

- **English and Vietnamese** (`NEXT_PUBLIC_LOCALE=auto|en|vi`, `auto` = the browser's languages; `<html lang>` follows). All text is in `src/web/src/lib/i18n.ts`; the words of the shared chat logic (`src/web/src/chat/chatState.ts`, identical in the Expo app) are translated in place by `applyChatText`. To add a language: one dictionary in `i18n.ts` (a test fails when a language misses a key). The Expo app is still English only.
- **Screen readers:** the message list is `role="log"` with `aria-live="off"` — an answer streams token by token and announcing every change is unusable. One polite `role="status"` region says the **finished** answer once (first 600 characters) or that an approval is needed (`src/web/src/components/announce.ts`); each message is read with who said it ("You:" / "Agent:"). Checked in a browser: `lang`, roles and Vietnamese text. Not checked with a real screen reader.

## Step 2 — Configure `.env.local`

| Variable | Value |
|---|---|
| `NEXT_PUBLIC_STREAMING`, `NEXT_PUBLIC_IDLE_TIMEOUT_S` | The **only** browser settings (public bundle — never a secret) |
| `BFF_AGENT_URL` | Runtime endpoint (`/agentbase-deploy`) or `http://127.0.0.1:8080` (`make dev`). Production: https |
| `BFF_AUTH_MODE` | `jwt` (OIDC sign-in) · `none` = local backend with `AUTH_MODE=none` (refused in production) |
| `BFF_RUNTIME_AUTH` | Runtime Inbound Auth `jwt` / *No authorization* ⇒ `jwt` (backend `AUTH_TOKEN_HEADER=Authorization`) · *IAM Permissions* ⇒ `iam` (backend `AUTH_TOKEN_HEADER=X-GreenNode-AgentBase-Custom-User-Token`) + `BFF_IAM_CLIENT_ID` / `BFF_IAM_CLIENT_SECRET` |
| `BFF_OIDC_ISSUER` / `CLIENT_ID` / `CLIENT_SECRET` / `SCOPES` / `AUDIENCE` | The IdP client of this app; per-IdP values as in `/agentbase-build-frontend` (Auth0 `AUDIENCE`, Cognito without `offline_access`). Confidential client recommended |
| `BFF_APP_ORIGIN` | Public origin, e.g. `https://chat.example.com` — redirect URI `<origin>/api/auth/callback`, post-logout URI `<origin>/`, CSRF Origin check |
| `BFF_SESSION_SECRET` | ≥ 32 random chars (`openssl rand -base64 48`) — encrypts the session cookie. The user sets it, never in chat |

The secrets (`BFF_OIDC_CLIENT_SECRET`, `BFF_IAM_CLIENT_SECRET`, `BFF_SESSION_SECRET`) go into the hosting platform's
secret store; production refuses http origins, `BFF_AUTH_MODE=none` and a short session secret (route answers 500,
reason in the server log). Details, IdP registration and deployment: `references/bff.md`.

## Flow

```
Browser ──(cookie + X-CSRF-Token, same origin)──▶ Next.js /api/* (BFF) ──(Bearer user token | IAM + user token)──▶ Runtime /invocations
  GET  /api/auth/login → IdP (code + PKCE, state, nonce) → GET /api/auth/callback → sealed session cookie → /
  GET  /api/auth/session → {user, csrfToken} (never a token)      POST /api/auth/logout → revoke + IdP end-session URL
  POST /api/invocations  → refresh if expiring, forward, pass SSE through unbuffered; 401 ⇒ refresh once + retry
```

## Structure (this skill's assets)

```
src/web/
├── next.config.ts                 # security headers on every response; locked-down CSP on /api/*
├── .env.example                   # NEXT_PUBLIC_* (browser) + BFF_* (server only)
├── tests/                         # vitest: SSE, reducer, contract, linkify, approval, cookies/CSRF/config, CSP
└── src/
    ├── proxy.ts, lib/csp.ts       # per-request nonce CSP for pages (strict-dynamic, no unsafe-inline)
    ├── app/                       # layout, page, globals.css, api/{auth/*,invocations}/route.ts
    ├── server/                    # BFF only: config, crypto (seal), cookies (chunks), csrf, session, oidc, iam, tokens, upstream
    ├── api/contract.ts, sse.ts    # = Expo copies (byte-identical, CI cmp)   api/agentClient.ts (adapted)
    ├── chat/chatState.ts          # = Expo copy                               chat/ConversationProvider.tsx (adapted)
    ├── components/linkify.ts      # = Expo copy   MessageBubble, ApprovalCard (+ approval.ts, pure)
    ├── auth/AuthProvider.tsx, screens/ChatScreen.tsx, App.tsx, config/env.ts
```

Shared with the Expo app **byte for byte** (`cmp` step in `.github/workflows/skills-ci.yml`): the contract types,
the SSE parser, the chat reducer and linkify — fix them in both skills together. Adapted (and why): `agentClient.ts`
calls same-origin `/api/invocations` with the cookie + CSRF header instead of `Authorization` (the browser holds no
token); `ConversationProvider.tsx` drops token refresh (the BFF refreshes and retries a 401 itself — a 401 reaching
the browser means "sign in again"); `ApprovalCard` / `MessageBubble` are DOM components (decision logic in `approval.ts`).

## Security rules

- **BFF model**: tokens only in a sealed (AES-256-GCM) **httpOnly, Secure, SameSite=Lax, `__Host-`** cookie, chunked
  when large. The browser never sees an access, refresh or ID token; `/api/auth/session` returns user + CSRF token only.
- **CSRF** on every POST: `Origin` = `BFF_APP_ORIGIN` (else `Sec-Fetch-Site: same-origin`) **and** `X-CSRF-Token` =
  the session's token (constant-time compare). No CORS headers anywhere.
- **No secrets in `NEXT_PUBLIC_*`** — they are inlined into public JavaScript. The agent URL is server-side too.
- Model output is **text**: React text nodes only — never `dangerouslySetInnerHTML` or an unsanitized markdown
  renderer. Links via `linkify` (https only) + `rel="noopener noreferrer nofollow"`, `target="_blank"`.
- **CSP**: pages get a per-request nonce (`src/web/src/proxy.ts`): `script-src 'nonce-…' 'strict-dynamic'`,
  `connect-src 'self'`, `frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'none'`; no inline `style` attributes.
- The BFF forwards only contract headers (never browser cookies), validates `X-GreenNode-AgentBase-Session-Id` and
  the body (known `type`, ≤ 64 KiB), and keeps the agent's status codes and error bodies (409 + `details.interrupt`).
- Threat model and checklist: `references/web-security.md`.

## Chat behavior

Identical to `/agentbase-build-frontend` → Rules (same reducer): status line, tokens, `reset`, tools, approval card
(`key={interrupt.id}`, Approve / Edit JSON / Reject with reason), 409 re-sync, failed resume ⇒ card again, Retry,
503 backoff, Stop, New conversation (new session id), 👍/👎 only with `trace_id` + `feedback_token`. The session id
is created in the browser; a reload starts a new conversation. Another user signing in ⇒ new conversation. A long
conversation is rotated by the backend (`SESSION_MAX_TURNS`): `chatState` adopts the `session_id` returned in
`done` / `interrupt` and sends it from then on — keep that if you rewrite the reducer.

## Tests & run

```bash
make dev                                   # backend :8080 (AUTH_MODE=none locally ⇒ BFF_AUTH_MODE=none)
make web-dev                               # http://localhost:3000 (from the project root)
make web-test && (cd src/web && npx next build)   # type-check + unit tests, then the production build
```

Verify by hand: streamed answer, Stop, tool line, approval card + resume, chat while an approval is pending re-shows
the card, 👍 creates a `user_feedback` score, a POST without `X-CSRF-Token` or from another origin ⇒ 403, Sign out →
Sign in asks for credentials again, the page's response has the nonce CSP and no token appears in DevTools storage.

## Official docs

- [create-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/create-runtime) — Runtime Inbound Auth: IAM ⇒ a BFF
- [Next.js Content Security Policy](https://nextjs.org/docs/app/guides/content-security-policy) · [Proxy](https://nextjs.org/docs/app/api-reference/file-conventions/proxy) · [Route Handlers](https://nextjs.org/docs/app/api-reference/file-conventions/route)
- [OAuth 2.0 for Browser-Based Applications (BFF)](https://datatracker.ietf.org/doc/draft-ietf-oauth-browser-based-apps/) · [OWASP CSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
