"""Toy target for tests/test_mutation_harness.py; small and pure, and mutated on purpose."""


def grade(score: int) -> str:
    if score >= 50:
        return "pass"
    return "fail"


def is_member(name, members):
    return name in members and name is not None


def describe(record):
    """Render the record's tag."""
    tag = record.get("@Tag", "none")
    return f"<{tag}>"


def tally(values, log):
    total = 0
    for value in values:
        total += value
    log.append(total)
    return total


def require(flag):
    if not flag:
        raise ValueError("flag required")
    return True


def sign(number):
    return "neg" if number < 0 else "pos"  # pragma: no mutate


if __name__ == "__main__":
    print(grade(99))
