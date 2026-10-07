# Definition of Done / Review checklist

Run each item and report `PASS` / `FAIL` / `N/A` with evidence (command + short output). Items marked **[B]** = BLOCKER, do not deploy if FAIL.

## Structure & env
- [B] `src/backend/pyproject.toml` + `uv.lock` + `.python-version` (3.13) exist; `requires-python = ">=3.13,<3.14"`; no `requirements.txt`; `uv sync --frozen` works; `uv run python --version` prints 3.13.x.
- [B] `app/` structure follows the standard; no second graph/LLM factory (`grep -rn "ChatOpenAI(" app | grep -v app/llm` is empty).
- [B] No `os.environ`/`os.getenv` reads outside `app/config.py`, `app/prompts`, `app/observability` (`grep -rn "os.getenv\|os.environ" app`).
- [B] `.env`, `.greennode.json` are in `.gitignore` and `.dockerignore`.
- [B] `make check-creds` prints OK for IAM and LLM (secrets were filled in by the user, never pasted into chat or printed).
- Every variable in `Settings` is in `.env.example`.

## Runtime contract
- [B] `GET /health` → 200; listens on `0.0.0.0:8080`.
- [B] `make test` green; `make lint` clean.
- Image built with `--platform linux/amd64`, runs as non-root.

## LLM
- [B] LLMs are created only via `get_llm(task)` (`app/llm`); `LLM_MODEL` is the model's `path` on AIP.
- Prod: the API key lives in the runtime's env file or Identity, not in the image.

- Tiers: `summarize`/`router` use the small tier (if configured); the `large` tier has **at least 1 fallback** from a different model family for prod; every tier's model has been run through `make eval`.
- Every LLM call goes through `get_llm("<task>")` (`grep -rn "ChatOpenAI(" app | grep -v app/llm` is empty).

## Memory
- [B] `MEMORY_BACKEND=agentbase` + `MEMORY_ID` in dev/staging/prod.
- [B] actor_id comes from `Principal`, not a tool parameter; namespace matches the memory store's `namespaceTemplate`.
- Compression thresholds tuned to the model's context window; long-conversation test passes.

## Tools / MCP
- [B] Side-effect tools are in `HITL_TOOLS` (or the user confirmed in writing that it is not needed).
- Prod MCP goes through the MCP Gateway, inbound `IAM` or `JWT`; secrets live in Identity (Secret Provider).
- [B] The Gateway has a **Policy Group** attached (none attached ⇒ every tools/call returns 403); there is an ALLOW policy for the agent's principal `iam:<sub>` (taken from the startup log) with the exact action `<connector>__<tool>`.
- [B] `mcp_servers.json` points to **each connector's connectUrl** (`<gateway endpoint>/<connector>`), not the gateway root.
- The main flow produces no `mcp.policy_denied` in traces.
- No duplicate tool names; a failing MCP server does not crash the agent (trace shows `mcp.list_tools` WARNING).

## Auth
- [B] `AUTH_MODE=jwt` outside local; issuer/audience configured; expired/wrong-audience token test ⇒ 401.
- [B] Spoofed `X-GreenNode-AgentBase-User-Id` header ⇒ 403.

## Tracing (Langfuse v4)
- [B] One request creates exactly 1 `agent.invoke` trace with user_id, session_id, tags, version.
- Complete trace tree: `auth`, `tools.collect`, `mcp.list_tools`, `compress/context.compress`, `recall/memory.recall`, generation (model, usage, cost), tool spans, `hitl.*` when present.
- Generations have cost (GreenNode model prices declared in Langfuse) and `input_cache_read` if the provider returns it.
- No tokens/API keys in traces (check 1 real trace).

## HITL
- Interrupt → approve/edit/reject works; chatting while approval is pending ⇒ 409.

## Evaluation
- [B] `evals/datasets/*.jsonl` covers the spec's main flows (≥10 items for prod).
- [B] `make eval` meets the agreed `--min-pass-rate`.
- 👍/👎 feedback from the frontend records a `user_feedback` score.

## Self-built MCP server (if any)
- `/health` is public; `/mcp` without a token ⇒ 401 (`WWW-Authenticate` includes `resource_metadata`).
- Per-user data tools use `current_user()` from the token; there is a 2-user isolation test.
- Root keys / OAuth clients live in Identity; env only holds hashes / public info.

## A2A (if enabled — with a reason per decision guide §5)
- Agent Card has the correct URL, securitySchemes match `AUTH_MODE`; `POST /a2a` without auth ⇒ 401.
- User B cannot GetTask user A's task (test `test_tasks_isolated_per_user`).
- Client: a separate key per target agent; `ask_*` tools pass the user; tools that can take actions are in `HITL_TOOLS`.

## Frontend (if any)
- No secrets in `EXPO_PUBLIC_*`; OIDC uses PKCE; tokens in SecureStore.
- All SSE events handled (`token`, `reset`, `interrupt`, `done`, `error`).

## Deploy
- The deploy env file does not contain `GREENNODE_CLIENT_ID/SECRET/AGENT_IDENTITY/ENDPOINT_URL`.
- [B] The deploy env file sets `APP_ENV` (dev/staging/prod) — `local` is refused on the Runtime.
- After deploy: health OK, 1 real request has a trace in Langfuse, clean logs (`/agentbase-monitor`).
