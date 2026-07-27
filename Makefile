.PHONY: setup upstreams bridge test lint typecheck validate doctor update clean

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
