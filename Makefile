# Portable Python discovery - tries python3.11, python3, then falls back to .venv
# Use .venv for test-security because it has the required dependencies
PYTHON_RUNTIME ?= $(shell which python3.11 2>/dev/null || which python3.10 2>/dev/null || which python3 2>/dev/null || echo /Users/raelldottin/.venv/bin/python)
PYTHON_AUTOMATION ?= $(shell which python3.11 2>/dev/null || which python3.10 2>/dev/null || which python3 2>/dev/null || echo /Users/raelldottin/.venv/bin/python)
PYTHON_TEST_SECURITY ?= /Users/raelldottin/.venv/bin/python

.PHONY: automation-check automation-dry-run syntax-check test test-security git-check

automation-check:
	PYTHONDONTWRITEBYTECODE=1 \
	$(PYTHON_AUTOMATION) -m unittest automation.tests.test_harness

automation-dry-run:
	PYTHONDONTWRITEBYTECODE=1 \
	$(PYTHON_AUTOMATION) automation/supervisor/run_next.py --dry-run

syntax-check:
	$(PYTHON_RUNTIME) -m compileall -q run.py sdk

test:
	$(PYTHON_RUNTIME) -m unittest discover -s tests -p 'test_*.py'

test-security:
	$(PYTHON_TEST_SECURITY) -m unittest discover -s tests -p 'test_security*.py'

git-check:
	git diff --check