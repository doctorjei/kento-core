.PHONY: test test-integration test-all

# Default: fast unit suite only. tests/integration/ is excluded by the
# addopts entry in pyproject.toml, so a bare `pytest tests/` picks up
# only the unit tests even if invoked directly.
test:
	PYTHONPATH=src python3 -m pytest tests/

# Tier 1 hook-execution harness. Runs real `sh` against generated hook
# scripts. See plans/lxc-in-lxc-tests.md for scope.
test-integration:
	PYTHONPATH=src python3 -m pytest tests/integration/ -v

# Both suites, unit first.
test-all: test test-integration
