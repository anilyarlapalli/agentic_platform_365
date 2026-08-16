.DEFAULT_GOAL := help
SHELL := /bin/bash
PY := .venv/bin/python
COMPOSE := docker compose -f deploy/compose.yml
COMPOSE_RUNTIME := docker compose -f deploy/compose.yml -f deploy/compose.runtime.yml

.PHONY: help up down destroy runtime-up runtime-down migrate init-store lock-check \
	lint audit policy verify chaos load mutate seed-chat api chat web web-install \
	web-build web-check worker seed-kg e2e-transport e2e-llm e2e-ingest \
	e2e-domain images image-audit backup restore-drill check evidence fmt logs psql

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Start the local substrate and wait for health
	@$(COMPOSE) up -d
	@echo "waiting for postgres…"
	@until $(COMPOSE) exec -T postgres pg_isready -U platform_owner -d platform >/dev/null 2>&1; \
		do sleep 1; done
	@echo "ready. jaeger :16687  grafana :3100  prometheus :9190  minio :9101"

down: ## Stop the substrate, keep volumes
	@$(COMPOSE) down

runtime-up: ## Build and start the role-separated local runtime
	@$(COMPOSE_RUNTIME) up -d --build --wait

runtime-down: ## Stop the role-separated runtime, keep volumes
	@$(COMPOSE_RUNTIME) down

destroy: ## Stop and delete volumes — irreversible
	@$(COMPOSE) down -v

migrate: ## Apply migrations as the owner role
	@$(PY) -m alembic upgrade head

init-store: ## Create the object store bucket (idempotent)
	@$(PY) -m scripts.init_object_store

lock-check: ## Verify Python dependency declarations match the lock file
	@uv lock --check --offline

lint: ## Lint without modifying source
	@.venv/bin/ruff check .

audit: ## Audit locked Python and web dependencies
	@.venv/bin/pip-audit --strict
	@cd web && npm audit --audit-level=high

policy: ## Validate hardened manifests, runbooks and secret hygiene
	@$(PY) -m scripts.check_deployment_policy

verify: ## Property tests — every platform guarantee (fast, ~1s)
	@$(PY) -m pytest tests/properties -q

chaos: ## SIGKILL the worker at every side-effect boundary (~35s)
	@$(PY) -m pytest tests/chaos -q

load: ## Throughput, contention and recovery — records to evidence/load/
	@$(PY) -m pytest tests/load -q

mutate: ## Break each control in turn; every one must turn the suite red (~3m)
	@$(PY) -m scripts.mutation_check

seed-chat: ## Seed two demo tenants with an embedded corpus (~$0.0002)
	@$(PY) -m scripts.seed_chat

api: ## Run the chat API on :8100
	@$(PY) -m uvicorn platform_core.api.app:app --host 127.0.0.1 --port 8100 --reload

chat: ## Interactive chat client against a running API
	@$(PY) -m scripts.chat_client

web: ## Console on :3000 — needs `make api` running on :8100
	@cd web && npm run dev

web-install: ## Install the console's dependencies
	@cd web && npm ci

web-build: ## Production build of the console (typechecks every page)
	@cd web && npm run build

web-check: ## Locked web audit, generated types, typecheck and build
	@cd web && npm ci --ignore-scripts
	@cd web && npm audit --audit-level=high
	@cd web && npm run typegen
	@cd web && npm run typecheck
	@cd web && npm run build

worker: ## Run a polling worker — required for onboarding drafts
	@$(PY) -m apps.worker.runner

seed-kg: ## Seed a corpus rich enough for onboarding to synthesise edge types
	@$(PY) -m scripts.seed_onboarding_corpus

e2e-transport: ## Real Redis + a real Celery worker, end to end
	@$(PY) -m scripts.e2e_transport

e2e-llm: ## One real OpenAI call, metered and attributed (costs ~$0.00002)
	@$(PY) -m scripts.e2e_llm

e2e-ingest: ## Upload → retained bytes → chunks → replace, live (costs ~$0.0001)
	@$(PY) -m scripts.e2e_ingest

e2e-domain: ## A new domain: upload → draft → approve → publish → chat (costs ~$0.05)
	@$(PY) -m scripts.e2e_domain

images: ## Build both hardened production images
	@docker build -t local-platform:check -f Dockerfile .
	@docker build -t local-platform-web:check -f web/Dockerfile web

image-audit: images ## Scan final OS and language packages for fixable high/critical CVEs
	@./scripts/scan_container_images.sh local-platform:check local-platform-web:check

backup: ## Create a local Postgres backup under backups/
	@./scripts/backup_local_postgres.sh

restore-drill: ## Destructively recreate only the isolated restore-drill database
	@./scripts/restore_drill_local_postgres.sh

check: lock-check lint policy verify chaos load web-check ## All unpaid release checks
	@echo "— all suites green —"

evidence: ## Show what is currently proven
	@$(PY) -m scripts.show_evidence

fmt: ## Format and lint
	@.venv/bin/ruff format . && .venv/bin/ruff check --fix .

logs: ## Tail the substrate logs
	@$(COMPOSE) logs -f --tail=50

psql: ## Shell as the APP role — RLS applies, which is the point
	@PGPASSWORD=platform_dev_only psql -h 127.0.0.1 -p 5442 -U platform_app -d platform
