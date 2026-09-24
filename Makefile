# PMTiles publishing platform — developer entry points.
#
# Everything runs locally through docker compose. `make up` is safe to re-run.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

COMPOSE_FILES := -f docker-compose.yml
ifeq ($(PROFILE),debug)
COMPOSE_FILES += -f docker-compose.debug.yml
endif
ifneq ($(PROFILE),)
COMPOSE_PROFILE_ARGS := --profile $(PROFILE)
endif

COMPOSE := docker compose $(COMPOSE_FILES) $(COMPOSE_PROFILE_ARGS)
export GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)

# Long-running services `up --wait` waits on. The one-shot init containers are
# excluded (compose treats any exited container as a failure) and are checked
# separately by scripts/check-oneshots.sh.
WAIT_SERVICES := postgres kafka s3 kafka-console auth
ONESHOTS := migrate kafka-init s3-init
export ONESHOTS
export COMPOSE_CMD := $(COMPOSE)

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / \
		{printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""
	@echo "  Add PROFILE=observability for prometheus/grafana/jaeger,"
	@echo "  or PROFILE=debug to publish internal ports on localhost."

# ---------------------------------------------------------------- lifecycle
.env: .env.example
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")
	@touch .env

dev-keys/internal-jwt.key:
	@./scripts/gen-dev-keys.sh

.PHONY: bootstrap
bootstrap: .env dev-keys/internal-jwt.key ## Create .env and dev keys (no containers)

.PHONY: up
up: bootstrap ## Build and start the stack, waiting until it is healthy
	$(COMPOSE) up -d --build
	$(COMPOSE) up -d --no-build --wait --wait-timeout 300 $(WAIT_SERVICES)
	@echo "--- one-shot jobs ---"
	@./scripts/check-oneshots.sh
	@$(MAKE) --no-print-directory status

.PHONY: down
down: ## Stop the stack, keeping data volumes
	$(COMPOSE) down --remove-orphans

.PHONY: clean
clean: ## Stop the stack, delete volumes and dev keys
	$(COMPOSE) down --remove-orphans --volumes
	rm -rf dev-keys
	@echo "removed volumes and dev keys"

.PHONY: restart
restart: ## Recreate one service: make restart s=worker
	@test -n "$(s)" || (echo "usage: make restart s=<service>" && exit 1)
	$(COMPOSE) up -d --build --force-recreate $(s)

.PHONY: status
status: ## Show container status
	@$(COMPOSE) ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'

.PHONY: logs
logs: ## Tail logs: make logs s=worker  (omit s for everything)
	$(COMPOSE) logs -f --tail=200 $(s)

.PHONY: psql
psql: ## Open a psql shell on the catalogue
	$(COMPOSE) exec -e PGPASSWORD=$$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-) \
		postgres psql -U postgres -d pmtiles

# ---------------------------------------------------------------- quality
.PHONY: lint
lint: ## ruff check + format check + mypy + tsc
	uv run ruff check packages services tests ops scripts
	uv run ruff format --check packages services tests ops scripts
	uv run mypy packages services ops
	cd services/auth && npm run --silent typecheck

.PHONY: fmt
fmt: ## Auto-fix lint and formatting
	uv run ruff check --fix packages services tests ops scripts
	uv run ruff format packages services tests ops scripts

.PHONY: test
test: ## Run the unit tests (no containers required)
	uv run pytest packages services -q
	cd services/auth && npm run --silent test --if-present

.PHONY: e2e
e2e: ## Run the end-to-end suite against the running stack
	uv run pytest tests/e2e -q -m e2e

