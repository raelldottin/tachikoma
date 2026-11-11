# Use .venv for all Python commands to ensure dependencies are available.
# The path is absolute because the test recipes leave the repository root.
PYTHON = $(shell if [ -x "$(CURDIR)/.venv/bin/python" ]; then echo "$(CURDIR)/.venv/bin/python"; else command -v python3 2>/dev/null || echo python3; fi)
# An interpreter given as PYTHON=... is made absolute as well: a path is resolved against the repository
# root, a bare command name through PATH.
TEST_PYTHON = $(if $(findstring /,$(PYTHON)),$(abspath $(PYTHON)),$(or $(shell command -v $(PYTHON) 2>/dev/null),$(PYTHON)))
# Tests that construct Device() or import run.py write ./.device and ./tachikoma.log, so the suites run from a
# fresh temporary directory that is removed afterwards. Never run them from the repository root
# (`python -m unittest discover -s tests`, `python tests/test_x.py`): that rewrites the real ./.device.
# tests/__init__.py refuses `python -m unittest tests.<module>` there, but covers the other forms only partly.
IN_TEMP_DIR = tmp=$$(mktemp -d) && [ -d "$$tmp" ] && trap 'rm -rf "$$tmp"' EXIT && cd "$$tmp" &&
UNITTEST_DISCOVER = "$(TEST_PYTHON)" -m unittest discover -s "$(CURDIR)/tests" -t "$(CURDIR)"

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
	$(IN_TEMP_DIR) $(UNITTEST_DISCOVER) -p 'test_*.py'

test-security:
	$(IN_TEMP_DIR) $(UNITTEST_DISCOVER) -p 'test_security*.py'

git-check:
	git diff --check

lint:
	uv run ruff check run.py sdk tests scripts
	uv run ty check --exit-zero run.py sdk tests

pre-commit: lint syntax-check test git-check
