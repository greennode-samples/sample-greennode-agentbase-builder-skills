---
name: agentbase-build-frontend
description: "React Native UI standard (Expo, TypeScript) for AI agents on GreenNode AgentBase in src/frontend: OIDC PKCE login (expo-auth-session) + SecureStore, client calling POST /invocations per the contract (session header, JWT), SSE streaming via expo/fetch, human-in-the-loop approval cards, 👍👎 feedback buttons sent to Langfuse. Use when the agent needs a mobile/web chat app, adding a chat screen, connecting a frontend to the agent, handling stream/interrupt on the client. Trigger: UI, frontend, React Native, Expo, mobile app for agent, chat interface, chat screen, app mobile cho agent, giao diện chat, màn hình chat. DO NOT use for the agent backend (use the other agentbase-build-* skills)."
---

# React Native Frontend (Expo)

Only create when the Agent Spec includes a UI. Fixed location: `src/frontend`.

## Step 1 — Create the app

```bash
bash <skill-dir>/scripts/setup_frontend.sh <project-name> <project>/src/frontend
```

Script: `npx create-expo-app@latest --template blank-typescript` (latest Expo at run time) → overlay `assets/frontend/` → `npx expo install expo-auth-session expo-web-browser expo-crypto expo-secure-store expo-constants` (lets Expo pick SDK-compatible versions — **don't** pin versions yourself) → set `scheme` in `app.json` for the OIDC redirect (kept if already set). (Called automatically when scaffolding with `--with-frontend`.)

Existing app (`<dir>/package.json` present): only the **missing** template files are added — your `App.tsx` / `src/…` are kept (each is listed as `kept existing …`). `--force` overwrites them with the template; review the diff afterwards.

## Step 2 — Configure `.env` (only `EXPO_PUBLIC_*` variables, NO secrets)

| Variable | Value |
|---|---|
| `EXPO_PUBLIC_AGENT_URL` | Runtime endpoint (prod) · `http://<LAN-IP>:8080` when running on a real device against a local backend |
| `EXPO_PUBLIC_AUTH_MODE` | `jwt` · `none` (only when the backend uses `AUTH_MODE=none` locally) |
| `EXPO_PUBLIC_AUTH_TOKEN_HEADER` | Matches the backend `AUTH_TOKEN_HEADER` |
| `EXPO_PUBLIC_OIDC_ISSUER` / `CLIENT_ID` / `SCOPES` / `AUDIENCE` | IdP public client + PKCE; register the redirect URIs below |
| `EXPO_PUBLIC_STREAMING` | `true` to use SSE |

Redirect URI — `makeRedirectUri({ path: 'auth' })` differs per runtime and the IdP matches it **exactly** (otherwise `invalid redirect_uri`). Register every one you use; in dev the app logs the current value (`[auth] redirect_uri to register at the IdP: …`, `__DEV__` only):

| Runs in | Redirect URI |
|---|---|
| Development build / store build | `<scheme>://auth` (`scheme` in `app.json`, = project name) |
| Expo Go | `exp://<LAN-IP>:8081/--/auth` — changes with the machine's IP/port; use a dev build for anything shared |
| Web (`npx expo start --web`) | `http://localhost:8081/auth` (+ allow web origin `http://localhost:8081`: the token call is a CORS request) |

Keycloak: `EXPO_PUBLIC_OIDC_ISSUER=https://<host>/realms/<realm>`, **public** client + PKCE, leave `EXPO_PUBLIC_OIDC_AUDIENCE` empty (audience is added by a Keycloak mapper, see `/agentbase-build-auth` patterns). In `jwt` mode the client does **not** send the User-Id header (the backend takes it from `sub`). Resume includes `interrupt_id` to prevent double approval.

## Structure (this skill's assets)

```
src/frontend/
├── App.tsx                    # AuthProvider → login screen | ChatScreen
└── src/
    ├── config/env.ts          # reads EXPO_PUBLIC_*
    ├── auth/AuthProvider.tsx  # OIDC PKCE, refresh token, SecureStore, userId = sub
    ├── api/agentClient.ts     # invoke()/invokeStream() (SSE) with chatBody()/resumeBody(), sendFeedback(trace, token)
    ├── components/MessageBubble.tsx, ApprovalCard.tsx
    └── screens/ChatScreen.tsx # session UUID per conversation, stream, HITL, feedback
```

## Rules

- Follow the contract in `/agentbase-build` → `references/api-contract.md` **exactly**; changing the contract requires updating the backend too.
- 1 conversation = 1 `session_id` (UUID, `expo-crypto`); the "New" button creates a new session (new short-term memory, long-term memory persists).
- Handle all SSE events: `token` (append text), `tool_start` (show tool), `reset` (clear text), `interrupt` (show ApprovalCard), `done` (store `trace_id` + `feedback_token`), `error` (`{message, status}`: 401 ⇒ sign out; 409 on resume ⇒ see below). Parse SSE per spec (`createSseParser`: LF/CRLF/CR, multi-line `data:`, last event flushed at EOF).
- Failed resume: **409** (approval already handled — e.g. a dropped stream whose resume did complete, a double submit — or expired) ⇒ drop the ApprovalCard and show a short note; re-showing it would 409 forever and lock the composer. Network / 5xx / dropped stream ⇒ show the card again to retry.
- "New" aborts the in-flight request (`AbortController`) and ignores its late callbacks, so an old `interrupt` never lands in the new session.
- Token refresh: sign out only when the IdP **rejects** the refresh token (`TokenError`, e.g. `invalid_grant`); offline / IdP 5xx keeps the session and `getAccessToken()` rejects (the turn shows the error, retry later). Tokens live in a ref, so a callback created before a refresh never reuses the rotated refresh token. `none` mode does no OIDC discovery at all.
- Tokens: SecureStore (native); web keeps them in memory only. Don't log tokens.
- Don't embed IAM credentials / LLM keys in the app (the bundle is public). Endpoint requires IAM ⇒ use a BFF (`/agentbase-build-auth` references/patterns.md).
- Streaming uses `fetch` from `expo/fetch` (supports `response.body.getReader()` on iOS/Android).

## Run & verify

```bash
make dev        # backend :8080
make fe-dev     # Expo; open with Expo Go / simulator
```

Links: `MessageBubble` makes `https://` URLs tappable (only https; trailing punctuation, markdown and inline-code backticks are not part of the link) — e.g. the `AUTHORIZATION_REQUIRED` consent link from `/agentbase-build-identity`: the user opens it, consents, comes back and sends the request again.

Verify: send a message (stream visible), tool calls displayed, HITL shows the approval card and resumes, 👍 creates a `user_feedback` score in Langfuse, expired tokens refresh automatically.

## Official docs

- [create-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/create-runtime) — Runtime Inbound Auth: JWT lets the app call directly, IAM requires a BFF
