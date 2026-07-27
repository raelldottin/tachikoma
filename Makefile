PYTHON_RUNTIME ?= /Users/raelldottin/.venv/bin/python
PYTHON_AUTOMATION ?= /Users/raelldottin/.venv/bin/python

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
	$(PYTHON_RUNTIME) -m unittest discover -s tests -p 'test_security*.py'

git-check:
	git diff --check