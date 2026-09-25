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

# The observability profile also switches tracing on everywhere: services only
# export spans when OTEL_EXPORTER_OTLP_ENDPOINT is set, and the edge only
# starts traces when EDGE_OTEL_TRACE=on.
ifeq ($(PROFILE),observability)
export OTEL_EXPORTER_OTLP_ENDPOINT := http://jaeger:4317
export EDGE_OTEL_TRACE := on
OBSERVABILITY_SERVICES := prometheus grafana jaeger
endif
export GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)

# Long-running services `up --wait` waits on. The one-shot init containers are
# excluded (compose treats any exited container as a failure); a failing one
# already fails the first `up`, because its dependants require
# `service_completed_successfully`.
WAIT_SERVICES := postgres kafka s3 kafka-console auth gateway backend worker edge-verifier edge

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
	$(COMPOSE) up -d --no-build --wait --wait-timeout 300 $(WAIT_SERVICES) $(OBSERVABILITY_SERVICES)
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

# ---------------------------------------------------------------- demo
AUTH_CLI := $(COMPOSE) run --rm --no-deps -T auth node dist/users-cli.js

.PHONY: add-tenant
add-tenant: ## Create a tenant: make add-tenant slug=acme name="Acme Corp"
	@test -n "$(slug)" || (echo 'usage: make add-tenant slug=acme [name="Acme Corp"]' && exit 1)
	@$(AUTH_CLI) add-tenant "$(slug)" "$(name)"

.PHONY: add-user
add-user: ## Create a user: make add-user email=a@b.c password=... tenant=tenant-a role=member
	@test -n "$(email)" -a -n "$(password)" || (echo 'usage: make add-user email=... password=... [tenant=slug] [role=owner|admin|member] [name="..."] [admin=1]' && exit 1)
	@$(AUTH_CLI) add-user "$(email)" "$(password)" \
		$(if $(tenant),--tenant "$(tenant)") $(if $(role),--role "$(role)") \
		$(if $(name),--name "$(name)") $(if $(admin),--platform-admin)

.PHONY: list-users
list-users: ## List users with their tenants and roles
	@$(AUTH_CLI) list

.PHONY: seed
seed: ## Create the demo tenants, users and producer API key
	@# Root inside this one-off container: with rootless Docker that is the host
	@# user, the only uid that can write dev-keys/. The file is 0644 like the
	@# other dev keys, so it stays readable under rootful Docker too.
	$(COMPOSE) run --rm --no-deps -T --user 0:0 -v "$(CURDIR)/dev-keys:/run/dev-keys" \
		-e PRODUCER_API_KEY_PATH=/run/dev-keys/producer-api-key auth node dist/seed.js

.PHONY: demo
demo: ## Publish a sample archive end to end and print the map URL
	./scripts/demo.sh $(f) $(slug)

.PHONY: synthetic
synthetic: ## Generate a synthetic PMTiles archive (needs no sample data)
	./scripts/make-synthetic-pmtiles.sh sample-data/synthetic.pmtiles

.PHONY: chaos-duplicate
chaos-duplicate: ## Replay a publication message; assert one version is created
	uv run pytest tests/e2e/test_chaos.py -k duplicate -v

.PHONY: chaos-crash-after-copy
chaos-crash-after-copy: ## Kill the worker between copy and commit; assert clean recovery
	uv run pytest tests/e2e/test_chaos.py -k crash -v

.PHONY: dlq
dlq: ## Print the dead-letter topic
	$(COMPOSE) exec -T kafka rpk topic consume publication.requested.dlq -o :end -f \
		'--- %k\n%v\n' || true

# ---------------------------------------------------------------- quality
.PHONY: lint
lint: ## ruff check + format check + mypy + tsc
	uv run ruff check packages services tests ops scripts
	uv run ruff format --check packages services tests ops scripts
	uv run mypy packages services ops
	cd services/auth && npm run --silent typecheck && npm run --silent format:check

.PHONY: fmt
fmt: ## Auto-fix lint and formatting
	uv run ruff check --fix packages services tests ops scripts
	uv run ruff format packages services tests ops scripts
	cd services/auth && npm run --silent format

.PHONY: event-schemas
event-schemas: ## Regenerate docs/events/*.schema.json from the Pydantic models
	uv run python scripts/export-event-schemas.py

.PHONY: test
test: ## Run the unit tests (no containers required)
	uv run pytest packages services -q
	node --test web/tests/
	cd services/auth && npm run --silent test --if-present

.PHONY: e2e
e2e: ## Run the end-to-end suite against the running stack
	uv run pytest tests/e2e -q -m e2e

