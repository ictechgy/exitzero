from dataclasses import dataclass


@dataclass
class Result:
    message: str
    kind: str
    order: tuple[str, ...]


def validate(result: Result) -> None:
    assert result.message == "invalid request"
    assert result.kind == "TypeError"
    assert result.order == ("type", "message", "order")


if __name__ == "__main__":
    validate(Result("invalid request", "ValueError", ("message", "type", "order")))
