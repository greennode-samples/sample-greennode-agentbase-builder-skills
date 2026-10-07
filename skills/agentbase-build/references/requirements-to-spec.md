# From business requirements → Agent Spec

The coding agent fills in this template **before writing code**, presents it to the user for confirmation, then saves it to the project's `docs/agent-spec.md`.

## Agent Spec template

```markdown
# Agent Spec — <project-name>

## 1. Goal
- Problem: ...
- Users: (internal / customers / partners) — estimated count: ...
- Success metric: (e.g. ≥80% of FAQ questions answered correctly, 30% fewer L1 tickets)

## 2. Channels & UI
- [ ] API only (called by another service)   - [ ] Mobile app (React Native)   - [ ] Web (Expo web)
- Streaming: yes / no

## 3. Authentication
- IdP: (Keycloak / Auth0 / Cognito / Entra ID ...) — issuer: ..., audience: ...  (GreenNode IAM does not publish JWKS ⇒ do not use it as the inbound IdP)
- Claims to pass to tools (`AUTH_FORWARD_CLAIMS`, e.g. `email`, `department`): ...
- Who may call the agent: ...

## 4. LLM
- Tier reasoning / large / small (path on AIP): ... / ... / ...   - Fallback per tier: ...
- Which flow uses which tier (agent, router, summarize, judge, eval_judge, new business flows): ...
- Adaptive routing: on / off — reason: ...
- Reply language: ... (default: the user's language)

## 5. Tools
| Tool | Source (local / MCP server via Gateway / POC mock) | Read/Write | Needs HITL? | Credential (Identity provider) |
|---|---|---|---|---|

## 6. Memory
- Short-term: keep the conversation per session (on by default)
- Long-term: what to remember about the user? (preferences, profile, decision history) — strategy: SEMANTIC / USER_PREFERENCE / CUSTOM
- Event retention (eventExpiryDuration): ... days
- Context budget: CONTEXT_MAX_TOKENS=..., KEEP_LAST=...

## 7. Human-in-the-loop
- Tools/actions needing approval: ...
- Approver: the user themself / someone else (if someone else ⇒ a separate approval flow is needed, describe it)

## 8. Evaluation
- 10–30 sample questions + expectations (put into evals/datasets/*.jsonl)
- pass_rate threshold for deploy: ...
- Self-eval loop: on / off — criteria: ...

## 9. Component decisions (per decision-guide.md)
| Component | On? | Reason |
|---|---|---|
| Long-term memory (strategy?) | | |
| Reflection loop | | |
| HITL (which tools, threshold?) | | |
| Build MCP server | | |
| A2A server / client | | |
| Frontend | | |
| MAX_TOOL_ROUNDS | | |

## 10. Non-functional
- Sensitive data to mask in traces: ...
- Network: Runtime PUBLIC / VPC (internal MCP server?)
- Expected load / autoscaling: ...
```

## Requirement → component map

| What you hear | Component | Skill |
|---|---|---|
| "remember the conversation", "follow-up questions" | Short-term memory (checkpointer) | memory |
| "remember my preferences/info next time" | Long-term memory + strategy | memory |
| "long conversations", "long documents", "tools return lots of data" | Compression + hard trim, tune thresholds | memory |
| "answer from internal documents/policies", "FAQ" | `search_knowledge` (local POC → MCP + vector store in prod), cite sources | mcp |
| "system X has no API yet, demo first" | Local mock adapter with the final tool name, blocked in prod | mcp |
| "look up system X", "call an internal API" | Build an MCP server for X + Custom Connector on the MCP Gateway + Policy Group | mcp-server + mcp |
| "delegate to another team's agent", "other agents call this agent" | A2A (only when decision guide §5 is satisfied) | a2a |
| "hard questions need careful thinking", "optimize LLM cost" | Tiers reasoning/large/small + adaptive routing | llm |
| "must not die when a model fails" | Per-tier fallback model | llm |
| "send email/create ticket/transfer money/delete" | Side-effect tool ⇒ `HITL_TOOLS` | hitl + mcp |
| "only department A staff may use tool B" | Gateway Policy Group (`/agentbase-policy`) + inbound JWT | mcp + auth |
| "use the user's Google/Slack account" | Identity OAuth2 3LO (`@requires_access_token` USER_FEDERATION) | auth |
| "mobile app", "chat interface" | Expo React Native | frontend |
| "answers must be accurate", "must not make things up" | Eval dataset + correctness judge, consider the self-eval loop | eval |
| "track cost/latency", "debug wrong answers" | Langfuse tracing + dashboard | tracing |
| "A/B prompts", "change prompts without deploying" | Langfuse Prompt Management (labels) | tracing |

## What does NOT change between projects

Directory structure · payload contract · how the LLM is obtained · checkpointer · LTM namespace · trace tree · JWT verification · Dockerfile · Makefile targets.
