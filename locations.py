"""
Amsterdam location pool for virtual charger placement.

Usage:
    from locations import get_location
    loc = get_location(index)  # cycles through pool via modulo
"""

from typing import TypedDict


class Location(TypedDict):
    display_name: str
    address: str
    city: str
    latitude: float
    longitude: float


# Empty by default — set demo_locations=true or populate via environment
_POOL: list[Location] = []


def get_location(index: int) -> Location:
    """Return a location from the pool, cycling via modulo.
    Returns empty Location if pool is empty."""
    if not _POOL:
        return Location(display_name="", address="", city="", latitude=0.0, longitude=0.0)
    return _POOL[index % len(_POOL)]
