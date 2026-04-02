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


# 30 real Amsterdam locations, spread across all districts
_POOL: list[Location] = [
    # ── Central ──────────────────────────────────────────────────────────────
    {
        "display_name": "Amsterdam Centraal",
        "address": "Stationsplein 15",
        "city": "Amsterdam",
        "latitude": 52.3791,
        "longitude": 4.9003,
    },
    {
        "display_name": "Dam Centrum",
        "address": "Dam 1",
        "city": "Amsterdam",
        "latitude": 52.3731,
        "longitude": 4.8932,
    },
    {
        "display_name": "Rokin Parkeergarage",
        "address": "Rokin 110",
        "city": "Amsterdam",
        "latitude": 52.3700,
        "longitude": 4.8940,
    },
    {
        "display_name": "Nieuwmarkt",
        "address": "Nieuwmarkt 4",
        "city": "Amsterdam",
        "latitude": 52.3726,
        "longitude": 4.9004,
    },
    # ── West / Jordaan ────────────────────────────────────────────────────────
    {
        "display_name": "Jordaan - Prinsengracht",
        "address": "Prinsengracht 263",
        "city": "Amsterdam",
        "latitude": 52.3741,
        "longitude": 4.8827,
    },
    {
        "display_name": "Oud-West - Kinkerstraat",
        "address": "Kinkerstraat 45",
        "city": "Amsterdam",
        "latitude": 52.3673,
        "longitude": 4.8737,
    },
    {
        "display_name": "Westerpark",
        "address": "Haarlemmerweg 8",
        "city": "Amsterdam",
        "latitude": 52.3877,
        "longitude": 4.8738,
    },
    {
        "display_name": "Bos en Lommer - Bosleeuw",
        "address": "Jan van Galenstraat 100",
        "city": "Amsterdam",
        "latitude": 52.3789,
        "longitude": 4.8620,
    },
    {
        "display_name": "Westermarkt",
        "address": "Westermarkt 20",
        "city": "Amsterdam",
        "latitude": 52.3752,
        "longitude": 4.8836,
    },
    # ── South / De Pijp / Zuidas ──────────────────────────────────────────────
    {
        "display_name": "De Pijp - Albert Cuypmarkt",
        "address": "Albert Cuypstraat 172",
        "city": "Amsterdam",
        "latitude": 52.3560,
        "longitude": 4.8978,
    },
    {
        "display_name": "Zuidas - Barbara Strozzilaan",
        "address": "Barbara Strozzilaan 201",
        "city": "Amsterdam",
        "latitude": 52.3374,
        "longitude": 4.8726,
    },
    {
        "display_name": "RAI Congresscentrum",
        "address": "Europaplein 24",
        "city": "Amsterdam",
        "latitude": 52.3396,
        "longitude": 4.8910,
    },
    {
        "display_name": "Rivierenbuurt - Amsteldijk",
        "address": "Amsteldijk 200",
        "city": "Amsterdam",
        "latitude": 52.3437,
        "longitude": 4.9052,
    },
    {
        "display_name": "Vondelpark Parkeergarage",
        "address": "Overtoom 65",
        "city": "Amsterdam",
        "latitude": 52.3613,
        "longitude": 4.8775,
    },
    {
        "display_name": "Oud-Zuid - Beethovenstraat",
        "address": "Beethovenstraat 29",
        "city": "Amsterdam",
        "latitude": 52.3454,
        "longitude": 4.8764,
    },
    # ── East ──────────────────────────────────────────────────────────────────
    {
        "display_name": "Oost - Wibautstraat",
        "address": "Wibautstraat 150",
        "city": "Amsterdam",
        "latitude": 52.3570,
        "longitude": 4.9148,
    },
    {
        "display_name": "Plantage - Artis",
        "address": "Plantage Kerklaan 38",
        "city": "Amsterdam",
        "latitude": 52.3664,
        "longitude": 4.9143,
    },
    {
        "display_name": "Science Park Amsterdam",
        "address": "Science Park 123",
        "city": "Amsterdam",
        "latitude": 52.3565,
        "longitude": 4.9515,
    },
    {
        "display_name": "Zeeburg - IJburg",
        "address": "Haringbuisdijk 1",
        "city": "Amsterdam",
        "latitude": 52.3563,
        "longitude": 4.9878,
    },
    {
        "display_name": "Indische Buurt - Javastraat",
        "address": "Javastraat 1",
        "city": "Amsterdam",
        "latitude": 52.3628,
        "longitude": 4.9347,
    },
    # ── North ─────────────────────────────────────────────────────────────────
    {
        "display_name": "NDSM Werf",
        "address": "TT. Neveritaweg 15",
        "city": "Amsterdam",
        "latitude": 52.4015,
        "longitude": 4.8974,
    },
    {
        "display_name": "Overhoeks - A'DAM Toren",
        "address": "Overhoeksplein 1",
        "city": "Amsterdam",
        "latitude": 52.3872,
        "longitude": 4.9007,
    },
    {
        "display_name": "Buikslotermeer Winkelcentrum",
        "address": "Buikslotermeerplein 34",
        "city": "Amsterdam",
        "latitude": 52.4091,
        "longitude": 4.9382,
    },
    {
        "display_name": "Noord - Meeuwenlaan",
        "address": "Meeuwenlaan 100",
        "city": "Amsterdam",
        "latitude": 52.3956,
        "longitude": 4.9221,
    },
    {
        "display_name": "Noord - Mosplein",
        "address": "Mosplein 1",
        "city": "Amsterdam",
        "latitude": 52.3999,
        "longitude": 4.9142,
    },
    # ── Nieuw-West ────────────────────────────────────────────────────────────
    {
        "display_name": "Osdorp - Osdorpplein",
        "address": "Osdorpplein 100",
        "city": "Amsterdam",
        "latitude": 52.3620,
        "longitude": 4.8142,
    },
    {
        "display_name": "Slotermeer - Burgemeester Röellstraat",
        "address": "Burgemeester Röellstraat 50",
        "city": "Amsterdam",
        "latitude": 52.3748,
        "longitude": 4.8282,
    },
    {
        "display_name": "Nieuw-West - Plein '40-'45",
        "address": "Plein '40-'45 1",
        "city": "Amsterdam",
        "latitude": 52.3699,
        "longitude": 4.8380,
    },
    {
        "display_name": "Sloterdijk Station",
        "address": "Orlyplein 1",
        "city": "Amsterdam",
        "latitude": 52.3887,
        "longitude": 4.8407,
    },
    # ── Southeast ─────────────────────────────────────────────────────────────
    {
        "display_name": "Bijlmer ArenA",
        "address": "Bijlmerdreef 1289",
        "city": "Amsterdam",
        "latitude": 52.3123,
        "longitude": 4.9422,
    },
    {
        "display_name": "Amstel Station",
        "address": "Julianaplein 1",
        "city": "Amsterdam",
        "latitude": 52.3464,
        "longitude": 4.9183,
    },
]


def get_location(index: int) -> Location:
    """Return a location from the pool, cycling via modulo."""
    return _POOL[index % len(_POOL)]
