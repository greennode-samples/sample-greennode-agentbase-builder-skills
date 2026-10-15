# IAM & permissions — who needs what

Sources: [getting-started](https://docs.greennode.ai/ai-stack/agent-base/getting-started), [roles-and-permissions](https://docs.greennode.ai/ai-stack/agent-base/team-permissions/roles-and-permissions), [manage-service-accounts](https://docs.greennode.ai/ai-stack/agent-base/team-permissions/manage-service-accounts), [manage-runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/manage-runtime).

## Principals in an agent project

| Principal | Created by | Used for | Permissions |
|---|---|---|---|
| **Team members** (people) | Root/Admin invite | Console work | Role: Root / Admin / Member / Viewer (matrix below) |
| **Developer service account** | You, in IAM — **one per person** | Local dev (`src/backend/.greennode.json`), `grn` CLI, the coding agent's platform skills | Policies `AgentBaseFullAccess` (Identity, Runtime, Memory), `vcrFullAccess` (registry), `AiPlatformFullAccess` (models, API keys) |
| **CI/CD service account** | You, in IAM | Build, push, deploy from the pipeline | A **dedicated** SA per project/environment, never a person's credentials; prefer a custom least-privilege policy (below) |
| **Runtime service account** `auto-sa-{agent-slug}` | Platform, when an agent is deployed | Injected into the runtime as `GREENNODE_CLIENT_ID/SECRET` (Memory, Identity, MCP Gateway IAM inbound) | Managed by the platform |
| **Gateway service account** `sa-gateway-<id>` | Platform, when a gateway is created | The gateway's own platform calls | Managed by the platform |
| **LLM API key** | `/agentbase-llm` (Access Control) | MaaS calls (`LLM_API_KEY`) | One per agent per environment, plus one for eval/CI, so Rate Limits and usage can be attached per key |

Only the three `*FullAccess` managed policies are documented for AgentBase. For narrower permissions (for example a deploy-only CI account), create a **custom policy** in the IAM Console (*Policies* → select the actions → attach it to the service account; the platform `/agentbase` skill's IAM setup points there). Pick actions from the Console list — don't guess names — and validate the account with one dev deploy before using it for prod.

## Teams

- **One developer service account per person** — never a shared one: audit logs and revocation stay per person (someone leaves ⇒ revoke one account), and the Memory API's limit of 10 concurrent requests applies **per account**, so a shared account makes developers (and eval runs) throttle each other.
- **Separate dev and prod MCP Gateways**, each with its own Policy Group (one group per gateway; plan for 10 policies per group — `/agentbase-policy` — although the MCP docs mention 20): dev policies can then allow developer principals without touching prod.
- Per environment: its own memory store, LLM API key (Rate Limit), runtime (its own `auto-sa-…` principal) and GitHub Environment secrets.

## Team roles (summary of the matrix)

| Action | Root | Admin | Member | Viewer |
|---|:-:|:-:|:-:|:-:|
| Create & edit agents, memory, access control | ✅ | ✅ | ✅ | ✕ |
| Delete agents, memory, access control | ✅ | ✅ | ✕ | ✕ |
| Tools & integrations (gateways, connectors): create/edit/delete | ✅ | ✅ | ✕ | ✕ |
| Create API key / view & delete API keys | ✅ / ✅ | ✅ / ✅ | ✅ / ✕ | ✕ |
| Rate Limit & model: create & edit | ✅ | ✅ | ✕ | ✕ |
| Budget: view & edit | ✅ | ✕ | ✕ | ✕ |
| Registry: delete images, reset credentials | ✅ | ✅ | ✕ | ✕ |
| View logs & traces, agents, tools | ✅ | ✅ | ✅ | ✅ |

Consequences for the build process:
- **Gateway, connector, policy and rate-limit steps need Admin** (or Root). A Member can build and deploy the agent but must ask an Admin for those.
- Deleting resources (teardown) needs Admin.

## Rules

- **Client secrets are shown once.** The runtime SA's secret appears only right after deploy; later only Root can view it in IAM, and each view is audit-logged. You don't need it: the runtime injects it.
- **Rotate / recover a runtime's credentials** with `PATCH /runtime/agent-runtimes/{id}/reset-service-account`. It regenerates `GREENNODE_CLIENT_ID/SECRET` and **restarts** the runtime.
- **Orphaned SAs:** deleting an agent leaves its SA marked *Orphaned*. Revoke it in IAM (part of `/agentbase-teardown`).
- **Policy principals:** a Policy Group grants `iam:<sub>`, where `<sub>` is the calling service account's user id, **not** the client id. Locally that is the developer's SA — `make check-creds` prints it from `.greennode.json`. On AgentBase it is the runtime's `auto-sa-…` — read it from the runtime logs (`IAM principal … iam:<sub>`, logged at startup by `log_iam_principal()` — only when an enabled `auth: "iam"` MCP server is configured and IAM credentials exist — without such a server there is no line and no policy to grant) after each first deploy / service-account reset, then add it with `/agentbase-policy` (`/agentbase-build` Step 9b).
- Never put a person's credentials in CI. Use `grn` profiles or env vars (`GRN_ACCESS_KEY_ID` / `GRN_SECRET_ACCESS_KEY`) from the CI secret store. `grn … -o json` prints secrets in clear text, so keep it out of CI logs.
- Never paste client secrets or API keys into a chat with the coding agent. The user fills `.greennode.json` / `.env` / CI secrets themselves.
