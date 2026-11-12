# Use .venv for all Python commands to ensure dependencies are available.
# The path is absolute because the test recipes leave the repository root.
PYTHON = $(shell if [ -x "$(CURDIR)/.venv/bin/python" ]; then echo "$(CURDIR)/.venv/bin/python"; else command -v python3 2>/dev/null || echo python3; fi)
# An interpreter given as PYTHON=... is made absolute as well: a path is resolved against the repository
# root, a bare command name through PATH.
TEST_PYTHON = $(if $(findstring /,$(PYTHON)),$(abspath $(PYTHON)),$(or $(shell command -v $(PYTHON) 2>/dev/null),$(PYTHON)))
# Tests that construct Device() or import run.py write ./.device and ./tachikoma.log, so the suites run from a
# fresh temporary directory that is removed afterwards, through scripts/ci.py test as CI runs them (it also
# rejects unmocked network connections). Never run them from the repository root (`python -m unittest
# discover -s tests`, `python tests/test_x.py`): that rewrites the real ./.device. tests/__init__.py refuses
# `python -m unittest tests.<module>` there, but covers the other forms only partly.
IN_TEMP_DIR = tmp=$$(mktemp -d) && [ -d "$$tmp" ] && trap 'rm -rf "$$tmp"' EXIT && cd "$$tmp" &&
CI_TEST = PYTHONDONTWRITEBYTECODE=1 "$(TEST_PYTHON)" "$(CURDIR)/scripts/ci.py" test
# Only test, test-security and lint need Python >= 3.11 (scripts/ci.py and the pinned CI tools).
REQUIRE_311 = "$(TEST_PYTHON)" -c 'import sys; sys.exit(sys.version_info < (3, 11) and "make: this target needs Python >= 3.11 (found " + sys.version.split()[0] + "); create .venv")'
# make lint mirrors the CI lint job, with the ruff version pinned there.
CI_REQUIREMENTS = .github/requirements-ci.txt

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
	@$(REQUIRE_311)
	$(IN_TEMP_DIR) $(CI_TEST)

test-security:
	@$(REQUIRE_311)
	$(IN_TEMP_DIR) $(CI_TEST) --names tests.test_security

git-check:
	git diff --check

# The ty check is advisory: its failure does not fail lint, and --frozen --no-sync leaves .venv and uv.lock alone.
lint:
	@$(REQUIRE_311)
	@$(PYTHON) -m ruff --version | grep -qx "ruff $$(sed -n 's/^ruff==//p' $(CI_REQUIREMENTS))" || { echo "make lint: install $(CI_REQUIREMENTS) into .venv"; exit 1; }
	$(PYTHON) scripts/ci.py lint
	$(PYTHON) -m ruff format --check scripts/ci.py tests/test_ci.py
	-uv run --frozen --no-sync ty check --exit-zero run.py sdk tests

pre-commit: lint syntax-check test git-check
