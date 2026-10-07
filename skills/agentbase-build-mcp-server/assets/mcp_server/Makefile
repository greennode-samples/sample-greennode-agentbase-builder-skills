# __SERVER_NAME__ — MCP server. All Python commands run through uv.
IMAGE ?= __SERVER_NAME__
TAG ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
URL ?= http://localhost:8080/mcp

.PHONY: setup dev test lint fmt lock tools call inspector docker-build docker-run

setup:            ## Install deps and create .env (local, MCP_AUTH_MODE=none) if missing
	uv sync
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

dev:              ## Run the server locally on :8080 (MCP endpoint /mcp)
	uv run python server.py

test:             ## End-to-end tests (real server + real MCP client)
	uv run pytest -q

lint:
	uv run ruff check . && uv run ruff format --check .

fmt:
	uv run ruff check --fix . && uv run ruff format .

lock:             ## Update uv.lock after changing dependencies
	uv lock

tools:            ## List tools of the running server (TOKEN=<api key | JWT> if auth is on)
	MCP_URL=$(URL) MCP_TOKEN=$(TOKEN) uv run python scripts/call_tool.py

call:             ## Call a tool: make call TOOL=add_note ARGS='{"text":"hi"}'
	MCP_URL=$(URL) MCP_TOKEN=$(TOKEN) uv run python scripts/call_tool.py $(TOOL) '$(or $(ARGS),{})'

inspector:        ## Interactive UI — connect with Streamable HTTP to $(URL)
	npx @modelcontextprotocol/inspector

docker-build:     ## Build a linux/amd64 image for AgentBase Runtime
	docker build --platform linux/amd64 -t $(IMAGE):$(TAG) .

docker-run:       ## Run the image locally with .env
	docker run --rm -p 8080:8080 --env-file .env $(IMAGE):$(TAG)
