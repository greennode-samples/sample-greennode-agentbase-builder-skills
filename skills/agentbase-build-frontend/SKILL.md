---
name: agentbase-build-frontend
description: "React Native UI standard (Expo, TypeScript) for AI agents on GreenNode AgentBase in src/frontend: OIDC PKCE login (expo-auth-session) + SecureStore, client calling POST /invocations per the contract (session header, JWT), SSE streaming via expo/fetch, human-in-the-loop approval cards, 👍👎 feedback buttons sent to Langfuse. Use when the agent needs a mobile/web chat app, adding a chat screen, connecting a frontend to the agent, handling stream/interrupt on the client. Trigger: UI, frontend, React Native, Expo, mobile app for agent, chat interface, chat screen, app mobile cho agent, giao diện chat, màn hình chat. DO NOT use for the agent backend (use the other agentbase-build-* skills)."
---

# React Native Frontend (Expo)

Only create when the Agent Spec includes a UI. Fixed location: `src/frontend`.

## Step 1 — Create the app

```bash
bash <skill-dir>/scripts/setup_frontend.sh <project-name> <project>/src/frontend [--no-install] [--force]
```

Tested with **Expo SDK 57** (expo 57.0.x, react-native 0.86.3, react 19.2.3, TypeScript 6.0) created by **create-expo 5.0.1** — the script pins both (override: `EXPO_SDK=<n>`, `CREATE_EXPO_VERSION=<v>`; another SDK is untested — run `npx tsc --noEmit` and test HITL/streaming). Needs network (template download) and **Node ^20.19.4 · ^22.13.0 · ^24.3.0 · ≥ 25** (React Native 0.86 engines; checked by the script).

What it does: `CI=1 npx create-expo@5.0.1 <dir> --template blank-typescript@sdk-57 --no-install --no-agents-md` (`CI=1`: no prompt — inside a git repo create-expo would otherwise ask about `git init` and hang) → deletes the `.git` create-expo makes outside a repo (a nested repo makes the root repo store `src/frontend` as a gitlink, never its files) → overlays `assets/frontend/` → `app.json`: `name`/`slug` = project name, `scheme` = project name (OIDC redirect `<name>://auth`), `ios.bundleIdentifier` / `android.package` = `APP_ID` (default `com.example.<name without ->` — **set your own reverse-domain id before the first store build**, e.g. `APP_ID=vn.acme.myagent`, or edit `app.json`) → `npx expo install expo-auth-session expo-web-browser expo-crypto expo-secure-store expo-constants` (Expo picks SDK-compatible versions — **don't** pin them yourself). Called automatically when scaffolding with `--with-frontend`.

- Project name: `^[a-z][a-z0-9-]{1,38}[a-z0-9]$` (used as URL scheme).
- Existing app (`<dir>/package.json` present): only the **missing** template files are added — your `App.tsx` / `src/…` are kept (each listed as `kept existing …`); existing `app.json` values are kept. `--force` overwrites the template files; review the diff afterwards.
- A run that failed after create-expo leaves `.agentbase-setup-pending`: the next run applies the template again (instead of keeping Expo's own `App.tsx`).
- Apps created by older versions of this script may contain create-expo's `AGENTS.md` / `CLAUDE.md` / `.claude/settings.json`: they tell coding agents to use Expo Router in `src/app/` (this template does not) and enable a third-party Claude Code plugin. The script reports them; review and delete them yourself.

## Step 2 — Configure `.env` (only `EXPO_PUBLIC_*` variables, NO secrets)

`.env` is for local development only (git-ignored). Release builds: see Step 3.

| Variable | Value |
|---|---|
| `EXPO_PUBLIC_AGENT_URL` | **Direct**: runtime endpoint (from `/agentbase-deploy`). **BFF**: the BFF base URL. Local backend: iOS simulator / web `http://127.0.0.1:8080`, Android emulator `http://10.0.2.2:8080` (or `adb reverse tcp:8080 tcp:8080` and keep 127.0.0.1), real device `http://<LAN-IP>:8080` — but a local backend with `AUTH_MODE=none` listens on 127.0.0.1 only (anyone on the LAN could otherwise act as any user): use `AUTH_MODE=jwt` locally (then it binds 0.0.0.0) or a tunnel. Release builds require `https://` |
| `EXPO_PUBLIC_AUTH_MODE` | `jwt` · `none` (only when the backend uses `AUTH_MODE=none` locally) |
| `EXPO_PUBLIC_AUTH_TOKEN_HEADER` | Header the **app** sends `Bearer <user JWT>` in: `Authorization` in both modes. Direct: = backend `AUTH_TOKEN_HEADER`. BFF: stays `Authorization` even though the backend uses `AUTH_TOKEN_HEADER=X-GreenNode-AgentBase-Custom-User-Token` — the BFF moves it there |
| `EXPO_PUBLIC_OIDC_ISSUER` / `CLIENT_ID` / `SCOPES` / `AUDIENCE` | IdP **public** client + PKCE (table below); register the redirect URIs below |
| `EXPO_PUBLIC_OIDC_EPHEMERAL_SESSION` | `true` ⇒ iOS private browser session (no SSO cookie shared with Safari) |
| `EXPO_PUBLIC_STREAMING` | `true` to use SSE |
| `EXPO_PUBLIC_IDLE_TIMEOUT_S` | Give up when nothing arrives for this long (re-armed on every chunk); default 200 = backend `REQUEST_TIMEOUT_S` (180) + margin |

**BFF mode** (endpoint requires IAM, `/agentbase-build-auth` references/patterns.md B): the BFF must expose the **same** `POST /invocations` contract (payloads, status codes, 409 bodies), forward `X-GreenNode-AgentBase-Session-Id`, and pass SSE through **unbuffered** (`X-Accel-Buffering: no`, no gzip) — otherwise tokens arrive in one block at the end and the idle timeout may fire.

| IdP | `EXPO_PUBLIC_OIDC_SCOPES` | `EXPO_PUBLIC_OIDC_AUDIENCE` | Sign-out |
|---|---|---|---|
| Keycloak (`ISSUER=https://<host>/realms/<realm>`) | `openid profile email offline_access` | empty (audience comes from an *Audience* mapper, `/agentbase-build-auth` patterns) | revoke + end session; *Valid post logout redirect URIs* = `+` (same as redirect URIs) |
| Auth0 | `openid profile email offline_access` (API: *Allow Offline Access*) | the API identifier (else the access token is opaque) | revoke + end session if *RP-Initiated Logout* discovery is on; add the redirect URIs to *Allowed Logout URLs* |
| Entra ID (`ISSUER=https://login.microsoftonline.com/<tenant>/v2.0`) | `openid profile email offline_access api://<api-app-id>/<scope>` (the resource is chosen by the scope) | empty | end session (no revocation endpoint); redirect URI = post-logout URI |
| Cognito | `openid profile email` — **no** `offline_access` (Cognito rejects it; the code flow returns a refresh token anyway) | empty | local only (+ revoke if the discovery lists `revocation_endpoint`): no `end_session_endpoint` in Cognito's discovery — the hosted-UI cookie stays (call its `/logout` if needed) |

Redirect URI — `makeRedirectUri({ path: 'auth' })` differs per runtime and the IdP matches it **exactly** (otherwise `invalid redirect_uri`). Register every one you use, as **redirect URI and post-logout redirect URI**; in dev the app logs the current value (`[auth] redirect_uri to register at the IdP: …`, `__DEV__` only):

| Runs in | Redirect URI |
|---|---|
| Development build / store build | `<scheme>://auth` (`scheme` in `app.json`, = project name) |
| Expo Go | `exp://<LAN-IP>:8081/--/auth` — changes with the machine's IP/port; use a dev build for anything shared |
| Web (`npx expo start --web`) | `http://localhost:8081/auth` (Keycloak too — `http://localhost:8081` alone does not match) + web origin `http://localhost:8081` at the IdP (the token call is a CORS request) |

Web also needs `npx expo install react-dom react-native-web` (once) and the backend `CORS_ALLOW_ORIGINS=["http://localhost:8081"]` (prod: your web origin). In `jwt` mode the client does **not** send the User-Id header (the backend takes it from `sub`).

## Step 3 — Release builds (EAS)

`EXPO_PUBLIC_*` are inlined at **build** time and `src/frontend/.env` is git-ignored ⇒ EAS Build does not upload it. Define them per environment, e.g. `eas env:create --environment production --name EXPO_PUBLIC_AGENT_URL --value https://… --visibility plaintext` (same for `AUTH_MODE`, `OIDC_*`), or `build.<profile>.env` in `eas.json`. A release build (`!__DEV__`) **throws at startup** when `EXPO_PUBLIC_AGENT_URL` is missing or not `https://`, or `ISSUER` / `CLIENT_ID` are empty in `jwt` mode — never silently calling `http://127.0.0.1:8080`.

## Structure (this skill's assets)

```
src/frontend/
├── App.tsx                        # AuthProvider → ConversationProvider → sign-in screen (errors shown) | ChatScreen
└── src/
    ├── config/env.ts              # reads EXPO_PUBLIC_*, release-build checks
    ├── auth/AuthProvider.tsx      # OIDC PKCE, refresh (single-flight, forced on 401), sign-out (revoke + end session)
    ├── api/contract.ts            # payload / response / event types, chatBody()/resumeBody(), interruptFrom()   (pure)
    ├── api/sse.ts                 # createSseParser                                                             (pure)
    ├── api/agentClient.ts         # invoke()/invokeStream() (idle timeout, stops at the final event), sendFeedback()
    ├── chat/chatState.ts          # chatReducer: every turn / interrupt / error rule                             (pure)
    ├── chat/ConversationProvider.tsx # runs turns: 401 refresh+retry, 503 backoff, Stop, New, sign-out
    ├── components/linkify.ts      # https link detection                                                         (pure)
    ├── components/MessageBubble.tsx, ApprovalCard.tsx
    └── screens/ChatScreen.tsx     # header (New / Sign out), messages, approval card, notice (Retry), composer (Send / Stop)
```

`(pure)` modules import nothing from React Native / Expo — unit-test them in plain Node (or jest-expo).

## Rules

- Follow the contract in `/agentbase-build` → `references/api-contract.md` **exactly**; changing the contract requires updating the backend too.
- 1 conversation = 1 `session_id` (UUID, `expo-crypto`); "New" creates a new session (new short-term memory, long-term memory persists) and aborts the in-flight request (its late callbacks are ignored).
- Handle all SSE events: `token` (append), `tool_start` (show tool), `reset` (clear text **and** tools), `interrupt` (ApprovalCard), `done` (store `trace_id` + `feedback_token`; keep the streamed text if `response` is empty), `error` (`{message, status, interrupt?}`). Parse SSE per spec (`createSseParser`: LF/CRLF/CR, multi-line `data:`, last event flushed at EOF).
- The first `done` / `interrupt` / `error` **ends the turn**: the client stops reading (`reader.cancel()`) and nothing after it — a late frame, a transport error while closing, an exception in a handler — may change the result or bring back an old approval card.
- **409 + `interrupt`** (HTTP `details.interrupt` or SSE `error.interrupt`: a chat while an approval is pending, a stale `interrupt_id`) ⇒ show the ApprovalCard **from that interrupt** (`key={interrupt.id}` resets its state); a blocked chat message goes back into the composer. Resumes may return another `interrupt` (new id) — keep handling until `done`. Resume always sends `interrupt_id`.
- 409 **without** `interrupt`: on a resume ⇒ the approval is no longer pending (handled by an attempt whose reply was lost, or expired) ⇒ drop the card, say so, chatting continues; on a chat ⇒ readable message + **Start new conversation** action (never the raw developer message).
- Failed resume (network, 5xx, **504**, dropped stream, Stop) ⇒ show the card again with "the previous attempt may have completed" — resubmitting is safe: if it already ran, the backend answers 409 and the client re-syncs.
- Failed chat ⇒ error in the bubble, the text goes back into the composer, **Retry** resends it. HTTP **503** (before the turn ran) is retried automatically with backoff (1 s, 3 s). **Stop** aborts the turn and keeps the session; an idle timeout (`EXPO_PUBLIC_IDLE_TIMEOUT_S`) ends a silent turn.
- **401** ⇒ one forced token refresh + retry; still 401 (or the IdP rejects the refresh token: `TokenError`, e.g. `invalid_grant`) ⇒ local sign-out. Offline / IdP 5xx keeps the session (the turn shows the error). The conversation lives above the auth gate: signing in again as the same user continues it; another user gets a new one.
- **Sign out** (header button): local tokens cleared first, then refresh token revoked (best effort, RFC 7009) and the IdP session ended (`end_session_endpoint` + `id_token_hint` + `post_logout_redirect_uri`) — otherwise the next "Sign in" logs straight back in. Sign-in errors (`invalid redirect_uri`, IdP unreachable) are shown on the sign-in screen.
- Feedback: 👍/👎 only when the answer has `trace_id` **and** `feedback_token`; a failed send reverts the selection and shows an error.
- Tokens: SecureStore (native); web keeps them in memory only. The `id_token` is only used as `id_token_hint`. Don't log tokens.
- Don't embed IAM credentials / LLM keys in the app (the bundle is public). Endpoint requires IAM ⇒ BFF (above).
- Streaming uses `fetch` from `expo/fetch` (supports `response.body.getReader()` on iOS/Android).
- Layout: top inset = `Constants.statusBarHeight`, a fixed bottom margin for the home indicator / gesture bar, `KeyboardAvoidingView behavior="padding"` on iOS **and** Android (SDK 57 is edge-to-edge: the window is not resized for the keyboard). For exact insets (3-button Android nav bar, landscape) add `react-native-safe-area-context` (`npx expo install`, `SafeAreaProvider` + `useSafeAreaInsets`).

## Run & verify

```bash
make dev        # backend :8080
make fe-dev     # Expo; open with Expo Go / simulator
cd src/frontend && npx tsc --noEmit
```

Links: `MessageBubble` makes `https://` URLs tappable and opens them in the in-app browser (`WebBrowser.openBrowserAsync`) — only https; trailing punctuation, unbalanced brackets (`[x](https://…)`), backticks and *paired* markdown emphasis are not part of the link, while `…/Foo_(bar)` and a trailing `_` inside the URL are kept. E.g. the `AUTHORIZATION_REQUIRED` consent link from `/agentbase-build-identity`: the user opens it, consents, comes back and sends the request again.

Verify: send a message (stream visible), Stop mid-answer, tool calls displayed, HITL shows the approval card (Approve / Edit JSON / Reject with reason) and resumes, chat while an approval is pending re-shows the card, 👍 creates a `user_feedback` score in Langfuse, expired tokens refresh automatically, Sign out → Sign in asks for credentials again.

## Official docs

- [create-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/create-runtime) — Runtime Inbound Auth: JWT lets the app call directly, IAM requires a BFF
- [Expo environment variables in EAS](https://docs.expo.dev/eas/environment-variables/)
