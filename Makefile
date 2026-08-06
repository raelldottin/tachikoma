# Use .venv for all Python commands to ensure dependencies are available
PYTHON = $(shell command -v .venv/bin/python 2>/dev/null || command -v python3 2>/dev/null || echo python3)

.PHONY: automation-check automation-dry-run syntax-check test test-security git-check lint pre-commit

automation-check:
	PYTHONDONTWRITEBYTECODE=1 \
	$(PYTHON) -m unittest automation.tests.test_harness

automation-dry-run:
	PYTHONDONTWRITEBYTECODE=1 \
	$(PYTHON) automation/supervisor/run_next.py --dry-run

syntax-check:
	$(PYTHON) -m compileall -q run.py sdk

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

test-security:
	$(PYTHON) -m unittest discover -s tests -p 'test_security*.py'

git-check:
	git diff --check

lint:
	uv run ruff check run.py sdk tests scripts
	uv run ty check --exit-zero run.py sdk tests

pre-commit: lint syntax-check test git-check