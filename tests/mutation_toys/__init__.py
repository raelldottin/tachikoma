"""Toy targets and suites for tests/test_mutation_harness.py.

The file names deliberately avoid the test_*.py discovery pattern, so `make test` and `scripts/ci.py test` never
collect these suites; the harness runs them by module name inside temporary copies of a toy repository.
"""
