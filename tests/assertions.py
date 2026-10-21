"""
Helper assertions for the test suite.
Replaces Python's built-in `assert` statement with explicit runtime checks
to avoid Bandit B101 (ast.Assert) static analysis alerts while preserving
rich error messages and full Pytest test execution.
"""
from typing import Any


def check_true(condition: Any, message: str = "Condition expected to be True") -> None:
    """Kiểm tra điều kiện là True (hoặc truthy)."""
    if not condition:
        raise AssertionError(message)


def check_false(condition: Any, message: str = "Condition expected to be False") -> None:
    """Kiểm tra điều kiện là False (hoặc falsy)."""
    if condition:
        raise AssertionError(message)


def check_equal(actual: Any, expected: Any, message: str = "") -> None:
    """Kiểm tra giá trị thực tế bằng với giá trị kỳ vọng."""
    if actual != expected:
        err = message or f"Expected {expected!r}, but got {actual!r}"
        raise AssertionError(err)


def check_not_equal(actual: Any, expected: Any, message: str = "") -> None:
    """Kiểm tra giá trị thực tế khác giá trị kỳ vọng."""
    if actual == expected:
        err = message or f"Expected value different from {expected!r}, but got {actual!r}"
        raise AssertionError(err)


def check_in(item: Any, container: Any, message: str = "") -> None:
    """Kiểm tra item nằm trong container."""
    if item not in container:
        err = message or f"Expected {item!r} to be in {container!r}"
        raise AssertionError(err)


def check_not_in(item: Any, container: Any, message: str = "") -> None:
    """Kiểm tra item KHÔNG nằm trong container."""
    if item in container:
        err = message or f"Expected {item!r} NOT to be in {container!r}"
        raise AssertionError(err)


def check_is_none(val: Any, message: str = "") -> None:
    """Kiểm tra giá trị là None."""
    if val is not None:
        err = message or f"Expected None, but got {val!r}"
        raise AssertionError(err)


def check_is_not_none(val: Any, message: str = "") -> None:
    """Kiểm tra giá trị khác None."""
    if val is None:
        err = message or "Expected non-None value, but got None"
        raise AssertionError(err)


def check_is_instance(obj: Any, cls: Any, message: str = "") -> None:
    """Kiểm tra obj là instance của cls."""
    if not isinstance(obj, cls):
        err = message or f"Expected instance of {cls}, got {type(obj)}"
        raise AssertionError(err)


def check_greater(a: Any, b: Any, message: str = "") -> None:
    """Kiểm tra a > b."""
    if not (a > b):
        err = message or f"Expected {a!r} > {b!r}"
        raise AssertionError(err)


def check_greater_equal(a: Any, b: Any, message: str = "") -> None:
    """Kiểm tra a >= b."""
    if not (a >= b):
        err = message or f"Expected {a!r} >= {b!r}"
        raise AssertionError(err)


def check_less(a: Any, b: Any, message: str = "") -> None:
    """Kiểm tra a < b."""
    if not (a < b):
        err = message or f"Expected {a!r} < {b!r}"
        raise AssertionError(err)
