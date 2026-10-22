.PHONY: up down migrate serve lint test test-db train bench
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
