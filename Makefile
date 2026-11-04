.PHONY: up down migrate serve lint test test-db test-aidb train bench
up:        ; docker compose up -d db
down:      ; docker compose down
migrate:   ; .venv/bin/paybroker migrate
serve:     ; .venv/bin/paybroker serve
lint:      ; .venv/bin/ruff check src tests scripts
test:      ; .venv/bin/pytest -q
# Needs POSTGRES_PASSWORD exported (set -a; . ./.env; set +a). Uses a throwaway database named paybroker_test.
test-db:   ; docker compose exec -T db psql -U paybroker -c "DROP DATABASE IF EXISTS paybroker_test" -c "CREATE DATABASE paybroker_test" && \
	TEST_DATABASE_URL=postgresql://paybroker:$$POSTGRES_PASSWORD@127.0.0.1:5435/paybroker_test .venv/bin/pytest -q
# Retrain the fraud model (1,000,000 synthetic rows by default; flags: scripts/train_model.py --help; needs: uv pip install -e ".[train]")
train:     ; .venv/bin/python scripts/train_model.py
bench:     ; .venv/bin/python scripts/bench.py
# AIDB tests (marker `aidb`) against a throwaway container from the AIDB image (build it from the ecosystem repository, docker/aidb-postgres).
# AIDB_CALLBACK_HOST must be an address of this machine the container can reach (Docker Desktop: 192.168.65.254).
AIDB_PORT ?= 55471
AIDB_NAME ?= paybroker-aidb-test
AIDB_CALLBACK_HOST ?= 192.168.65.254
test-aidb:
	-docker stop $(AIDB_NAME) >/dev/null 2>&1
	docker run -d --rm --name $(AIDB_NAME) -p 127.0.0.1:$(AIDB_PORT):5432 -e POSTGRES_PASSWORD=aidb-test-throwaway aidb-postgres:17 -c aidb.allow_insecure_egress=on >/dev/null  # pragma: allowlist secret
	@for i in $$(seq 1 40); do docker exec $(AIDB_NAME) pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done; sleep 3
	AIDB_TEST_URL=postgresql://postgres:aidb-test-throwaway@127.0.0.1:$(AIDB_PORT)/postgres AIDB_TEST_CALLBACK_HOST=$(AIDB_CALLBACK_HOST) .venv/bin/pytest -q -m aidb; rc=$$?; docker stop $(AIDB_NAME) >/dev/null; exit $$rc  # pragma: allowlist secret
