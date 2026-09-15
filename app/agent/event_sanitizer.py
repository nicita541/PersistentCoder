from __future__ import annotations


SENSITIVE_KEYS = frozenset(
    {"content", "prompt", "messages", "secret", "token", "password"}
)


def sanitize_event_payload(value: object, *, depth: int = 0) -> object:
    if depth > 4:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:400]
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for raw_key, item in list(value.items())[:40]:
            key = str(raw_key)[:80]
            if key.casefold() in SENSITIVE_KEYS:
                result[key] = "[redacted]"
            else:
                result[key] = sanitize_event_payload(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [
            sanitize_event_payload(item, depth=depth + 1)
            for item in list(value)[:50]
        ]
    return str(value)[:400]
