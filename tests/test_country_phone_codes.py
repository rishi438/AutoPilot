from utils.country_phone_codes import country_phone_catalog, resolve_country_phone


def test_complete_catalog_contains_unique_supported_countries() -> None:
    catalog = country_phone_catalog()

    assert len(catalog) >= 240
    assert len({entry["alpha2"] for entry in catalog}) == len(catalog)
    assert resolve_country_phone("India") == {
        "name": "India",
        "alpha2": "IN",
        "alpha3": "IND",
        "dial_code": "+91",
    }


def test_common_country_aliases_resolve_to_calling_codes() -> None:
    assert resolve_country_phone("USA")["dial_code"] == "+1"
    assert resolve_country_phone("UK")["dial_code"] == "+44"
    assert resolve_country_phone("South Korea")["dial_code"] == "+82"
    assert resolve_country_phone("Japan")["dial_code"] == "+81"
