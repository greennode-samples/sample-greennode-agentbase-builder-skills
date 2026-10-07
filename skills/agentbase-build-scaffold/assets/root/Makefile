# __PROJECT_NAME__ — standard commands. All Python commands run through uv.
BACKEND := src/backend
FRONTEND := src/frontend
IMAGE ?= __PROJECT_NAME__
TAG ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
ENV_FILE ?= $(BACKEND)/.env
# eval concurrency: MaaS allows 10 requests/min per account (all models) — keep 1 unless whitelisted
CONCURRENCY ?= 1

.PHONY: setup check-creds dev test eval eval-push lint fmt lock docker-build docker-run fe-setup fe-dev invoke

setup:            ## Install backend deps (+ frontend if present)
	cd $(BACKEND) && uv sync
	@if [ -d $(FRONTEND) ]; then cd $(FRONTEND) && npm install; fi

check-creds:      ## Verify IAM / LLM / Langfuse credentials (prints OK/FAIL only, never secrets)
	cd $(BACKEND) && uv run python -m scripts.check_creds

dev:              ## Run the agent locally on :8080
	cd $(BACKEND) && uv run python main.py

test:             ## Unit test offline
	cd $(BACKEND) && uv run pytest -q

eval:             ## Offline eval on the local dataset (Langfuse Experiment if keys are set): make eval MIN=0.8 CONCURRENCY=1
	cd $(BACKEND) && uv run python -m evals.run_eval --data $(or $(DATA),evals/datasets/smoke.jsonl) --min-pass-rate $(or $(MIN),0.8) --concurrency $(CONCURRENCY)

eval-push:        ## Sync a JSONL to a Langfuse Dataset (named after the file), then run a Dataset Run: make eval-push DATA=evals/datasets/ltm.jsonl
	cd $(BACKEND) && uv run python -m evals.run_eval --data $(or $(DATA),evals/datasets/smoke.jsonl) --push --dataset __PROJECT_NAME__-$(basename $(notdir $(or $(DATA),evals/datasets/smoke.jsonl))) --concurrency $(CONCURRENCY)

lint:
	cd $(BACKEND) && uv run ruff check . && uv run ruff format --check .

fmt:
	cd $(BACKEND) && uv run ruff check --fix . && uv run ruff format .

lock:             ## Update uv.lock after changing dependencies
	cd $(BACKEND) && uv lock

docker-build:     ## Build a linux/amd64 image for AgentBase Runtime
	docker build --platform linux/amd64 -t $(IMAGE):$(TAG) $(BACKEND)

docker-run:       ## Run the image locally: make docker-run ENV_FILE=src/backend/.env.prod (default .env)
	@# GREENNODE_AGENT_IDENTITY simulates the Runtime, so a deploy env file without APP_ENV fails here, not on the platform
	docker run --rm -p 127.0.0.1:8080:8080 --env-file $(ENV_FILE) $(if $(filter %/.env,$(ENV_FILE)),,-e GREENNODE_AGENT_IDENTITY=docker-run-smoke) $(IMAGE):$(TAG)

invoke:           ## Try the local agent: make invoke MSG="hello"
	curl -s -X POST http://127.0.0.1:8080/invocations \
	  -H "Content-Type: application/json" \
	  -H "X-GreenNode-AgentBase-Session-Id: local-session-1" \
	  -H "X-GreenNode-AgentBase-User-Id: local-user" \
	  -d '{"type":"chat","message":"$(or $(MSG),hello)"}' | (cd $(BACKEND) && uv run python -m json.tool)

fe-setup:         ## Install required Expo packages for src/frontend (needs network)
	cd $(FRONTEND) && npm install && npx expo install expo-auth-session expo-web-browser expo-crypto expo-secure-store expo-constants

fe-dev:           ## Run the Expo dev server
	cd $(FRONTEND) && npx expo start
