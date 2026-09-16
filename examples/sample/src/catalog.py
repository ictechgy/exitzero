"""Small public contract used by executable review checks."""


def normalize_items(items: list[str]) -> list[str]:
    if not all(isinstance(item, str) for item in items):
        raise TypeError("items must contain only strings")
    return sorted(set(item.strip().lower() for item in items))
