"""Toy target whose statement-deletion mutant never terminates; only the timeout test runs it."""


def count_down(n):
    steps = 0
    while n > 0:
        n -= 1
        steps += 1
    return steps
