"""Curated 193 UN members plus the two UN non-member observer states."""

from __future__ import annotations

import pycountry

COUNTRY_CODES = frozenset(
    """
    AD AE AF AG AL AM AO AR AT AU AZ BA BB BD BE BF BG BH BI BJ BN BO BR BS BT BW BY BZ
    CA CD CF CG CH CI CL CM CN CO CR CU CV CY CZ DE DJ DK DM DO DZ EC EE EG ER ES ET
    FI FJ FM FR GA GB GD GE GH GM GN GQ GR GT GW GY HN HR HT HU ID IE IL IN IQ IR IS
    IT JM JO JP KE KG KH KI KM KN KP KR KW KZ LA LB LC LI LK LR LS LT LU LV LY MA MC
    MD ME MG MH MK ML MM MN MR MT MU MV MW MX MY MZ NA NE NG NI NL NO NP NR NZ OM PA
    PE PG PH PK PL PS PT PW PY QA RO RS RU RW SA SB SC SD SE SG SI SK SL SM SN SO SR
    SS ST SV SY SZ TD TG TH TJ TL TM TN TO TR TT TV TZ UA UG US UY UZ VA VC VE VN VU
    WS YE ZA ZM ZW
    """.split()
)

_LABELS = {
    "BO": "Bolivia",
    "BN": "Brunei",
    "CD": "Democratic Republic of the Congo",
    "CG": "Congo",
    "CI": "Côte d’Ivoire",
    "CN": "China",
    "FM": "Micronesia",
    "GM": "Gambia",
    "GW": "Guinea-Bissau",
    "IR": "Iran",
    "KP": "North Korea",
    "KR": "South Korea",
    "LA": "Laos",
    "MD": "Moldova",
    "MM": "Myanmar",
    "NL": "Netherlands",
    "PS": "Palestine",
    "RU": "Russia",
    "SY": "Syria",
    "TR": "Türkiye",
    "TZ": "Tanzania",
    "US": "United States",
    "VA": "Vatican City (Holy See)",
    "VE": "Venezuela",
    "VN": "Vietnam",
}
_ALIASES = {
    "uk": "GB",
    "usa": "US",
    "united states of america": "US",
    "uae": "AE",
    "south korea": "KR",
    "north korea": "KP",
    "vatican": "VA",
    "holy see": "VA",
}


def _country_label(code: str) -> str:
    if code in _LABELS:
        return _LABELS[code]
    country = pycountry.countries.get(alpha_2=code)
    if country is None:
        raise ValueError(f"Unknown country code in curated list: {code}")
    return country.name


def country_options() -> list[dict[str, str]]:
    """Return 195 country/observer options, alphabetically sorted by display label."""
    options = [
        {
            "code": code,
            "name": _country_label(code),
        }
        for code in COUNTRY_CODES
    ]
    return sorted(options, key=lambda item: item["name"].casefold())


def country_code(value: str) -> str | None:
    """Map a country name or code to the selector's ISO alpha-2 set."""
    clean = value.strip()
    if not clean:
        return None
    code = _ALIASES.get(clean.casefold())
    if code:
        return code
    try:
        result = pycountry.countries.lookup(clean).alpha_2
    except LookupError:
        return None
    return result if result in COUNTRY_CODES else None


def country_name(value: str) -> str:
    code = country_code(value)
    if code is None:
        return value.strip()
    return _country_label(code)
