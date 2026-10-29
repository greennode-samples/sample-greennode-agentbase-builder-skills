# __SERVER_NAME__ — MCP server. All Python commands run through uv.
IMAGE ?= __SERVER_NAME__
TAG ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
# PORT=8081 when a local agent (`make dev` of the agent) already uses 8080: `make dev PORT=8081`, `make tools PORT=8081`
URL ?= http://localhost:$(or $(PORT),8080)/mcp
# A variable given on the make command line only (NAME, LIST… may also exist in the environment, e.g. WSL sets NAME)
cli = $(if $(filter command line,$(origin $(1))),$($(1)))

.PHONY: setup set-secret dev test lint fmt lock tools call inspector docker-build docker-run docker-smoke

setup:            ## Install deps and create .env (local, MCP_AUTH_MODE=none) if missing
	uv sync
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

set-secret:       ## YOU run it in your own terminal: hidden prompt → .env (MCP_BACKEND_TOKEN…), never echoed: make set-secret [NAME=…] [ENV_FILE=.env.<env>] [LIST=1]
	@# python3 + stdlib only (works before `make setup`). STDIN=1 reads ONE value from a pipe (secret managers), YES=1 replaces it
	@python3 scripts/set_secret.py --env-file "$(or $(call cli,ENV_FILE),.env)" --name "$(call cli,NAME)" $(if $(call cli,LIST),--list) $(if $(call cli,STDIN),--stdin) $(if $(call cli,YES),--yes)

dev:              ## Run the server locally on :8080 (MCP endpoint /mcp); PORT=8081 to run next to a local agent
	$(if $(PORT),MCP_PORT=$(PORT) MCP_RESOURCE_URL=http://localhost:$(PORT)/mcp) uv run python server.py

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

docker-smoke:     ## Check the built image: refuses to start without auth, 401 without a token (needs Docker)
	bash scripts/smoke_image.sh $(IMAGE):$(TAG)

docker-run:       ## Run the image locally with .env, published on this machine's loopback only
	docker run --rm -p 127.0.0.1:$(or $(PORT),8080):8080 --env-file .env -e MCP_HOST=0.0.0.0 -e MCP_DB_PATH=/tmp/mcp.db $(IMAGE):$(TAG)
