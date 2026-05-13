# r64-db-engine — local development orchestration.
#
# Targets:
#   make dev-up           Start ephemeral postgres (docker, port 5433).
#   make dev-down         Stop the ephemeral postgres.
#   make seed             Seed 50K rows per table covering SPEC §6.1 type categories.
#   make test             Run unit tests (pytest, no --integration).
#   make test-integration Run pytest --integration (needs Docker).
#   make demo             One-shot: dev-up -> seed -> run --once -> verify ramdb.
#   make clean            Stop docker + remove /tmp/r64-demo artifacts.
#
# Demo writes to /tmp/r64-demo so it stays out of the repo. After `make demo`
# you can `ls /tmp/r64-demo/ramdb/PostgresSource/` to see the produced files.

SHELL          := /usr/bin/env bash
PYTHON         ?= python3
PG_PORT        ?= 5433
PG_USER        ?= postgres
PG_PASSWORD    ?= row64dev
PG_DATABASE    ?= analytics
SEED_ROWS      ?= 50000

DEMO_ROOT      := /tmp/r64-demo
DEMO_LOADING   := $(DEMO_ROOT)/ramdb
DEMO_STATE     := $(DEMO_ROOT)/state
DEMO_OUT       := $(DEMO_LOADING)/PostgresSource/Customers.ramdb
DEMO_CONFIG    := examples/minimal.yaml

.PHONY: help dev-up dev-down seed test test-integration demo clean

help:
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

dev-up: ## Start ephemeral postgres on localhost:5433
	@./scripts/dev_postgres.sh start

dev-down: ## Stop the ephemeral postgres container
	@./scripts/dev_postgres.sh stop

seed: ## Seed 50K rows per table across all type categories
	@PG_HOST=localhost PG_PORT=$(PG_PORT) PG_USER=$(PG_USER) \
	    PG_PASSWORD=$(PG_PASSWORD) PG_DATABASE=$(PG_DATABASE) \
	    $(PYTHON) scripts/seed_postgres.py --rows $(SEED_ROWS)

test: ## Run unit tests (no docker required)
	@$(PYTHON) -m pytest

test-integration: ## Run pytest --integration (testcontainers)
	@$(PYTHON) -m pytest --integration

demo: ## End-to-end: dev-up, seed, run --once, verify ramdb file
	@set -euo pipefail; \
	  echo "[demo] starting ephemeral postgres on :$(PG_PORT)"; \
	  ./scripts/dev_postgres.sh start >/dev/null; \
	  echo "[demo] seeding $(SEED_ROWS) rows per table"; \
	  PG_HOST=localhost PG_PORT=$(PG_PORT) PG_USER=$(PG_USER) \
	      PG_PASSWORD=$(PG_PASSWORD) PG_DATABASE=$(PG_DATABASE) \
	      $(PYTHON) scripts/seed_postgres.py --rows $(SEED_ROWS); \
	  echo "[demo] preparing loading dir $(DEMO_LOADING)"; \
	  mkdir -p $(DEMO_LOADING) $(DEMO_STATE); \
	  echo "[demo] validating $(DEMO_CONFIG)"; \
	  r64-db-engine validate --config $(DEMO_CONFIG); \
	  echo "[demo] running r64-db-engine --once"; \
	  r64-db-engine run --once --config $(DEMO_CONFIG); \
	  echo "[demo] checking for $(DEMO_OUT)"; \
	  test -s $(DEMO_OUT) || { \
	      echo "[demo] FAIL: $(DEMO_OUT) not found or empty"; \
	      ls -la $(DEMO_LOADING)/PostgresSource/ 2>/dev/null || true; \
	      ./scripts/dev_postgres.sh stop >/dev/null; \
	      exit 1; \
	  }; \
	  echo; \
	  echo "[demo] success — produced files:"; \
	  ls -lh $(DEMO_LOADING)/PostgresSource/; \
	  echo; \
	  echo "[demo] stopping ephemeral postgres (artifacts kept under $(DEMO_ROOT))"; \
	  ./scripts/dev_postgres.sh stop >/dev/null; \
	  echo "[demo] done. Run 'make clean' to remove $(DEMO_ROOT)."

clean: ## Stop docker and remove /tmp/r64-demo artifacts
	@./scripts/dev_postgres.sh stop || true
	@rm -rf $(DEMO_ROOT)
	@echo "[clean] removed $(DEMO_ROOT)"
