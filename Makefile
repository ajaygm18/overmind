.PHONY: setup upstreams bridge dev-up dev-down dev-logs test lint typecheck validate doctor update clean

# Every upstream is installed from its own registry. Overmind vendors nothing
# and forks nothing, so `make update` is the whole upgrade path (ADR-001).
upstreams:
	@echo "==> omnigent (execution + sandbox + policies)"
	uv tool install --python 3.12 omnigent || pip install omnigent
	@echo "==> open-multi-agent (planning) via the bridge"
	cd bridge && npm install
	@echo "==> ruflo (vector memory only; see ADR-002)"
	npx --yes ruflo@latest --version || true

setup: upstreams
	uv pip install -e ".[dev]" || pip install -e ".[dev]"
	@echo
	@echo "now run: make bridge   (in one terminal)"
	@echo "then:    overmind doctor"

bridge:
	cd bridge && npm start

# Containerised bridge + Jaeger. Optional: everything works with `make bridge`
# and no Docker. See docs/DEVELOPMENT.md for what is deliberately not in here.
dev-up:
	@test -f .env || { echo "no .env -- run: cp .env.example .env, then add the planner's provider key"; exit 1; }
	docker compose up -d --build
	@echo
	@echo "bridge:     http://127.0.0.1:7801/health"
	@echo "jaeger UI:  http://127.0.0.1:16686"
	@echo "now run:    overmind doctor"

dev-down:
	docker compose down

dev-logs:
	docker compose logs -f bridge

test:
	pytest -q

lint:
	ruff check overmind tests
	ruff format --check overmind tests

typecheck:
	mypy overmind
	cd bridge && npm run typecheck

# The exit condition for ExitKind.SCHEMA_VALID nodes.
validate:
	python -c "import overmind.config as c; c.load(); print('config valid')"

doctor:
	overmind doctor

update:
	uv tool upgrade omnigent || pip install -U omnigent
	cd bridge && npm update @open-multi-agent/core
	@echo "re-run `make test` -- the contract tests are what catch upstream drift"

clean:
	overmind gc || true
	rm -rf .mypy_cache .ruff_cache .pytest_cache
