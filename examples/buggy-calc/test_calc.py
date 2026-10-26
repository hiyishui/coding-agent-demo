import pytest
from calc import add, average, factorial, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(10, 4) == 6


def test_average():
    assert average([1, 2, 3, 4]) == 2.5
    with pytest.raises(ValueError):
        average([])


def test_factorial():
    assert factorial(0) == 1
    assert factorial(1) == 1
    assert factorial(5) == 120
