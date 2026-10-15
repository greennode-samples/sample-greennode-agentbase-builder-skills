# AgentBase manifest for `grn agentbase deploy up` — rendered in CI (.github/workflows/ci.yml): envsubst fills the
# non-secret ${…} values below, then deploy/render_runtime_spec.py completes runtime.env from the committed
# src/backend/.env.<env>.example plus the secrets of the GitHub Environment. Commit THIS template only; the rendered
# file contains secrets (temp dir, deleted after the run).
#
# name = shared join key of identity + runtime = <AGENT_NAME>-<env> (3–50 chars, ^[a-zA-Z0-9_-]+$): ONE identity per
# environment — never share one between dev/staging/prod (a dev caller could fetch prod users' 3LO tokens / delegated
# keys). The identity is ALWAYS created/kept with this name — it is the identity this environment's Access Control
# providers must belong to (/agentbase-build-identity).
#
# Not expressible here (grn has no fields for them): network mode (VPC), IP Access Control (allowedCidrs), Runtime
# Inbound Auth (IAM/JWT), wallet (POC/real). A runtime created by `deploy up` is Public with "No authorization"; set
# those in the Console or with /agentbase-deploy — and then CI refuses `runtime update` for that environment
# (grn would drop them), see /agentbase-build-deploy.
name: "${RUNTIME_NAME}"
description: "${AGENT_NAME} ${APP_ENV} (deployed by CI)"
identity:
  # Add this environment's IDENTITY_CALLBACK_URL here when tools use per-user credentials (3LO / delegated)
  allowedReturnUrls: []
# memory: intentionally omitted — the memory store is created once with /agentbase-memory and referenced by
# MEMORY_ID / MEMORY_STRATEGY_ID in .env.<env>.example (its namespaceTemplate must be
# /strategies/{memoryStrategyId}/actors/{actorId}).
runtime:
  image: "${IMAGE}"
  imageAuth: auto # resolve pull credentials from your vCR robot account
  command: ["python", "main.py"] # same as the Dockerfile CMD
  args: []
  flavorId: "${RUNTIME_FLAVOR}"
  # Thresholds must be within 25–75 % (runtime-reference); replicas 1–10. max > 1 ⇒ MEMORY_MAX_CONCURRENCY ≈ 10/max
  autoscaling: {minReplicas: 1, maxReplicas: 1, cpuUtilization: 70, memoryUtilization: 70}
  env:
    APP_ENV: "${APP_ENV}" # required — APP_ENV=local is refused on the Runtime
    AGENT_VERSION: "${AGENT_VERSION}" # = image tag ⇒ Langfuse `release`
    # Everything else comes from src/backend/.env.<env>.example (non-secret) and the CI secrets — do not add keys
    # here. Never GREENNODE_CLIENT_ID / _SECRET / GREENNODE_AGENT_IDENTITY — the Runtime injects them.
