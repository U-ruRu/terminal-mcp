"""Human presentation only; Mesh routing and access identities stay unchanged."""

from terminal_mcp.core.nato_names import _NATO, nato_name


def number_agent_name(number: str) -> str:
    """Map the negotiated public session number to the established NATO ordinal.

    Leading zeros are significant for number routing, but the ordinal is numeric.
    Every displayed name includes its cycle, including the first cycle.
    """
    if len(number) != 4 or not number.isascii() or not number.isdigit():
        raise ValueError("session number must contain four decimal digits")
    ordinal = int(number) + 1
    name = nato_name(ordinal)
    return f"{name}-1" if ordinal <= len(_NATO) else name


def valid_agent_name(name: str) -> bool:
    if not isinstance(name, str):
        return False
    word, separator, cycle = name.rpartition("-")
    return bool(
        separator and word in _NATO and cycle.isascii() and cycle.isdigit() and int(cycle) > 0
    )


def full_agent_name(name: str, label: str | None = None) -> str:
    """Append an existing slot label using the documented ' - ' separator."""
    label = (label or "").strip()
    return f"{name} - {label}" if label else name
