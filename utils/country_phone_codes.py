"""Canonical country names and calling codes from maintained metadata packages."""

from __future__ import annotations

from functools import lru_cache
import re
from typing import TypedDict

import phonenumbers
import pycountry


class CountryPhoneEntry(TypedDict):
    name: str
    alpha2: str
    alpha3: str
    dial_code: str


_MANUAL_ALIASES = {
    "america": "US",
    "britain": "GB",
    "england": "GB",
    "great britain": "GB",
    "ivory coast": "CI",
    "russia": "RU",
    "south korea": "KR",
    "north korea": "KP",
    "taiwan": "TW",
    "tanzania": "TZ",
    "uk": "GB",
    "united states of america": "US",
    "usa": "US",
    "vatican city": "VA",
    "vietnam": "VN",
}


def _normalize_country(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


@lru_cache(maxsize=1)
def _catalog_and_aliases() -> tuple[tuple[CountryPhoneEntry, ...], dict[str, str]]:
    entries: list[CountryPhoneEntry] = []
    aliases: dict[str, str] = {}
    for country in pycountry.countries:
        alpha2 = country.alpha_2.upper()
        calling_code = phonenumbers.country_code_for_region(alpha2)
        if calling_code <= 0:
            continue
        entry: CountryPhoneEntry = {
            "name": country.name,
            "alpha2": alpha2,
            "alpha3": country.alpha_3.upper(),
            "dial_code": f"+{calling_code}",
        }
        entries.append(entry)
        for candidate in (
            country.name,
            getattr(country, "official_name", ""),
            getattr(country, "common_name", ""),
            country.alpha_2,
            country.alpha_3,
        ):
            if candidate:
                aliases[_normalize_country(candidate)] = alpha2

    # libphonenumber supports Kosovo even though ISO 3166 does not assign it an
    # official alpha-2 code in pycountry.
    if "XK" in phonenumbers.SUPPORTED_REGIONS:
        entries.append(
            {"name": "Kosovo", "alpha2": "XK", "alpha3": "XKX", "dial_code": "+383"}
        )
        aliases["kosovo"] = "XK"
        aliases["xk"] = "XK"

    aliases.update(
        {_normalize_country(name): alpha2 for name, alpha2 in _MANUAL_ALIASES.items()}
    )
    entries.sort(key=lambda item: item["name"].casefold())
    return tuple(entries), aliases


def country_phone_catalog() -> list[CountryPhoneEntry]:
    """Return the complete supported country/calling-code catalog."""
    entries, _ = _catalog_and_aliases()
    return [dict(entry) for entry in entries]  # type: ignore[misc]


def resolve_country_phone(value: str | None) -> CountryPhoneEntry | None:
    """Resolve a country name or ISO code to its canonical name and dial code."""
    raw = (value or "").strip()
    if not raw:
        return None
    entries, aliases = _catalog_and_aliases()
    alpha2 = aliases.get(_normalize_country(raw))
    if alpha2 is None:
        return None
    return next((dict(entry) for entry in entries if entry["alpha2"] == alpha2), None)  # type: ignore[return-value]
