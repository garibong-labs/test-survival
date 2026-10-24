"""Environmental failure matching is explicit, case-sensitive substring matching."""


def matches(message, patterns):
    return any(pattern in message for pattern in patterns)
