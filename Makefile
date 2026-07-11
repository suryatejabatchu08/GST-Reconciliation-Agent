# ==============================================================
# GST Reconciliation Agent — Makefile
# Cross-platform convenience commands (requires GNU Make)
# Windows: install via choco install make  OR  use start_dev.ps1
# ==============================================================

.DEFAULT_GOAL := help
VENV := .venv/Scripts/python.exe
ifeq ($(OS),Windows_NT)
	VENV := .venv/Scripts/python.exe
else
	VENV := .venv/bin/python
endif

# ── Help ────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  GST Reconciliation Agent — Available Commands"
	@echo "  =============================================="
	@echo "  make validate   Check .env configuration"
	@echo "  make test       Run all 93 tests"
	@echo "  make start      Start all services (dev mode)"
	@echo "  make stop       Stop all running services"
	@echo "  make health     Check health of all services"
	@echo "  make docker-up  Start via Docker Compose"
	@echo "  make docker-down Stop Docker Compose"
	@echo "  make logs       Tail all service logs"
	@echo ""

# ── Env validation ───────────────────────────────────────────
validate:
	$(VENV) scripts/validate_env.py

# ── Testing ──────────────────────────────────────────────────
test:
	$(VENV) -m pytest \
		ingestion_service/tests/ \
		orchestration_service/tests/ \
		notification_service/tests/ \
		report_service/tests/ \
		gateway_service/tests/ \
		-v --tb=short

test-fast:
	$(VENV) -m pytest \
		ingestion_service/tests/ \
		orchestration_service/tests/ \
		notification_service/tests/ \
		report_service/tests/ \
		gateway_service/tests/ \
		-x --tb=short -q

# ── Local dev (no Docker) ────────────────────────────────────
start: validate
	@mkdir -p reports logs
	@$(VENV) -m uvicorn ingestion_service.main:app     --port 8001 --reload &
	@$(VENV) -m uvicorn orchestration_service.main:app --port 8002 --reload &
	@$(VENV) -m uvicorn notification_service.main:app  --port 8003 --reload &
	@$(VENV) -m uvicorn report_service.main:app        --port 8004 --reload &
	@$(VENV) -m uvicorn gateway_service.main:app       --port 8080 --reload &
	@echo "All services started. Gateway: http://localhost:8080"

stop:
	-pkill -f "uvicorn.*main:app" 2>/dev/null || true
	@echo "All services stopped."

health:
	@for port in 8001 8002 8003 8004 8080; do \
		if curl -sf http://localhost:$$port/health > /dev/null; then \
			echo "  ✓ Port $$port — UP"; \
		else \
			echo "  ✗ Port $$port — DOWN"; \
		fi; \
	done

# ── Docker ───────────────────────────────────────────────────
docker-up: validate
	docker-compose up --build -d
	@echo "All services running via Docker. Gateway: http://localhost:8080"

docker-down:
	docker-compose down

docker-restart:
	docker-compose down && docker-compose up --build -d

logs:
	docker-compose logs -f

logs-gateway:
	docker-compose logs -f gateway

# ── Cleanup ──────────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache

.PHONY: help validate test test-fast start stop health docker-up docker-down docker-restart logs logs-gateway clean
