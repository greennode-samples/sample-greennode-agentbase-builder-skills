# Live datasets — not gated

`make eval` and the CI gate run every `*.jsonl` directly in `evals/datasets/`, offline and in process. Put here the
items that need **deployed services**: MCP servers limited to deployed environments, `user_jwt` servers, a dev MCP
Gateway, test accounts in a real system. They are not part of the gate; run them by hand against dev:

```bash
make eval DATA=evals/datasets/live/<file>.jsonl
```

Point them at dev/test backends only — the eval runs the agent's tools for real (approved HITL tools included).
