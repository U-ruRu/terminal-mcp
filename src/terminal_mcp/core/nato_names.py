"""Established NATO alphabet and ordinal semantics shared by presentation and authority."""

_NATO = (
    "Alpha",
    "Bravo",
    "Charlie",
    "Delta",
    "Echo",
    "Foxtrot",
    "Golf",
    "Hotel",
    "India",
    "Juliett",
    "Kilo",
    "Lima",
    "Mike",
    "November",
    "Oscar",
    "Papa",
    "Quebec",
    "Romeo",
    "Sierra",
    "Tango",
    "Uniform",
    "Victor",
    "Whiskey",
    "X-ray",
    "Yankee",
    "Zulu",
)


def nato_name(ordinal: int) -> str:
    if ordinal < 1:
        raise ValueError("ordinal must be positive")
    word = _NATO[(ordinal - 1) % len(_NATO)]
    cycle = (ordinal - 1) // len(_NATO) + 1
    return word if cycle == 1 else f"{word}-{cycle}"
