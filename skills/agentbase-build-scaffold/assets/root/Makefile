# __PROJECT_NAME__ — standard commands. All Python commands run through uv.
BACKEND := src/backend
FRONTEND := src/frontend
WEB := src/web
IMAGE ?= __PROJECT_NAME__
# Image tag = AGENT_VERSION baked into the image = Langfuse `release` (git short SHA, or e.g. TAG=1.4.0)
TAG ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
ENV_FILE ?= $(BACKEND)/.env
# eval concurrency: MaaS allows 10 requests/min per account (all models) — keep 1 unless whitelisted
CONCURRENCY ?= 1
# A variable given on the make command line only (NAME, LIST… may also exist in the environment, e.g. WSL sets NAME)
cli = $(if $(filter command line,$(origin $(1))),$($(1)))

.PHONY: setup set-secret check-creds check-env security-scan dev test eval eval-push lint fmt lock docker-build docker-run fe-setup fe-dev web-setup web-dev web-test invoke

setup:            ## Install backend deps (+ frontend / web app if present)
	cd $(BACKEND) && uv sync
	@if [ -d $(FRONTEND) ]; then cd $(FRONTEND) && npm install; fi
	@if [ -d $(WEB) ]; then cd $(WEB) && npm install; fi

set-secret:       ## YOU run it in your own terminal: hidden prompt → .greennode.json / .env (never echoed): make set-secret [NAME=LLM_API_KEY|iam|langfuse] [ENV_FILE=src/backend/.env.<env>] [LIST=1]
	@# python3 + stdlib only: works before `make setup`. STDIN=1 reads ONE value from a pipe (secret managers), YES=1 replaces it
	@python3 $(BACKEND)/scripts/set_secret.py --env-file "$(ENV_FILE)" --name "$(call cli,NAME)" $(if $(call cli,LIST),--list) $(if $(call cli,STDIN),--stdin) $(if $(call cli,YES),--yes)

check-creds:      ## Verify IAM / LLM / Langfuse credentials (prints OK/FAIL only, never secrets)
	cd $(BACKEND) && uv run python -m scripts.check_creds

check-env:        ## Lint a deploy env file for the platform's literal KEY=VALUE parser: make check-env ENV_FILE=src/backend/.env.prod
	@case "$(ENV_FILE)" in */.env) echo "Pass a deploy env file: make check-env ENV_FILE=$(BACKEND)/.env.<env> (or .env.<env>.example)" >&2; exit 2;; esac
	uv run --project $(BACKEND) python deploy/env_file.py check $(ENV_FILE)

security-scan:    ## Security quick scan, offline (prints key names, never values): make security-scan [ENV=prod] [JSON=1]
	cd $(BACKEND) && uv run python -m scripts.security_scan $(if $(ENV),--env $(ENV)) $(if $(JSON),--json)

dev:              ## Run the agent locally on :8080
	cd $(BACKEND) && uv run python main.py

test:             ## Unit test offline
	cd $(BACKEND) && uv run pytest -q

eval:             ## Eval gate: every evals/datasets/*.jsonl must reach MIN (ltm.jsonl with LTM on; live/ by hand): make eval MIN=0.8 DATA=<file>
	cd $(BACKEND) && uv run python -m evals.run_eval --data $(or $(DATA),evals/datasets) --min-pass-rate $(or $(MIN),0.8) --concurrency $(CONCURRENCY)

eval-push:        ## Sync a JSONL to a Langfuse Dataset (named after the file), then run a gated Dataset Run: make eval-push DATA=evals/datasets/ltm.jsonl MIN=0.8
	cd $(BACKEND) && uv run python -m evals.run_eval --data $(or $(DATA),evals/datasets/smoke.jsonl) --push --dataset __PROJECT_NAME__-$(basename $(notdir $(or $(DATA),evals/datasets/smoke.jsonl))) --min-pass-rate $(or $(MIN),0.8) --concurrency $(CONCURRENCY)

lint:
	cd $(BACKEND) && uv run ruff check . && uv run ruff format --check .

fmt:
	cd $(BACKEND) && uv run ruff check --fix . && uv run ruff format .

lock:             ## Update uv.lock after changing dependencies
	cd $(BACKEND) && uv lock

docker-build:     ## Build a linux/amd64 image for AgentBase Runtime (AGENT_VERSION = TAG)
	docker build --platform linux/amd64 --build-arg AGENT_VERSION=$(TAG) -t $(IMAGE):$(TAG) $(BACKEND)

docker-run:       ## Run the image locally: make docker-run ENV_FILE=src/backend/.env.prod (default .env; lint deploy files with check-env first)
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

web-setup:        ## Install the Next.js web app's packages for src/web (needs network; vitest pinned like setup_web.sh)
	cd $(WEB) && npm install && npm install --save-dev --save-exact vitest@4.1.12

web-dev:          ## Run the Next.js web chat (BFF) on :3000
	cd $(WEB) && npm run dev

web-test:         ## Type-check + unit tests of src/web (offline)
	cd $(WEB) && npx tsc --noEmit && npm test
