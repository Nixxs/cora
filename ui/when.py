"""Short spans of time for the dashboard's lines: '5m', '3h ago', 'just now'."""


def whole_unit(seconds: float) -> str | None:
    """The largest whole unit, like '5m', '3h' or '2d'; None under a minute."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return None


def ago(then, now) -> str:
    """How long ago: 'never', 'just now', '5m ago', '3h ago', '2d ago'."""
    if then is None:
        return "never"
    span = whole_unit((now - then).total_seconds())
    return f"{span} ago" if span else "just now"
