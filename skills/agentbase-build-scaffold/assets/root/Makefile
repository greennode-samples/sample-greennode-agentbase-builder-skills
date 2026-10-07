# __PROJECT_NAME__ — standard commands. All Python commands run through uv.
BACKEND := src/backend
FRONTEND := src/frontend
IMAGE ?= __PROJECT_NAME__
TAG ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)

.PHONY: setup dev test eval eval-push lint fmt lock docker-build docker-run fe-setup fe-dev invoke

setup:            ## Install backend deps (+ frontend if present)
	cd $(BACKEND) && uv sync
	@if [ -d $(FRONTEND) ]; then cd $(FRONTEND) && npm install; fi

dev:              ## Run the agent locally on :8080
	cd $(BACKEND) && uv run python main.py

test:             ## Unit test offline
	cd $(BACKEND) && uv run pytest -q

eval:             ## Offline eval on the local dataset (Langfuse Experiment if keys are set)
	cd $(BACKEND) && uv run python -m evals.run_eval --data $(or $(DATA),evals/datasets/smoke.jsonl) --min-pass-rate $(or $(MIN),0.8)

eval-push:        ## Sync the dataset to Langfuse, then run a Dataset Run
	cd $(BACKEND) && uv run python -m evals.run_eval --data $(or $(DATA),evals/datasets/smoke.jsonl) --push --dataset __PROJECT_NAME__-smoke

lint:
	cd $(BACKEND) && uv run ruff check . && uv run ruff format --check .

fmt:
	cd $(BACKEND) && uv run ruff check --fix . && uv run ruff format .

lock:             ## Update uv.lock after changing dependencies
	cd $(BACKEND) && uv lock

docker-build:     ## Build a linux/amd64 image for AgentBase Runtime
	docker build --platform linux/amd64 -t $(IMAGE):$(TAG) $(BACKEND)

docker-run:       ## Run the image locally with .env
	docker run --rm -p 8080:8080 --env-file $(BACKEND)/.env $(IMAGE):$(TAG)

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
