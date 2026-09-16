from calculator import add


def test_adds_values():
    result = add(2, 3)
    assert result == 5
