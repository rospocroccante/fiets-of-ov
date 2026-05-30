"""Unit tests for the place-resolver's coordinate parsing (offline).

`_try_parse_latlon` decides whether a `from`/`to` value is already coordinates or a name to
geocode. It must accept only finite, in-range pairs — anything else (junk, out-of-range,
NaN/Inf) returns None so it falls through to geocoding instead of being stored as a corrupt
point.
"""

from app.services.places import _try_parse_latlon


def test_valid_coordinates_parse():
    assert _try_parse_latlon("52.37,4.90") == (52.37, 4.90)


def test_a_place_name_is_not_coordinates():
    assert _try_parse_latlon("Vondelpark") is None


def test_non_finite_is_rejected():
    assert _try_parse_latlon("nan,inf") is None
    assert _try_parse_latlon("1.0,inf") is None


def test_out_of_range_is_rejected():
    assert _try_parse_latlon("999,999") is None  # lat and lon both out of range
    assert _try_parse_latlon("-91,0") is None  # latitude below -90
    assert _try_parse_latlon("0,181") is None  # longitude above 180


def test_range_boundaries_are_accepted():
    assert _try_parse_latlon("-90,-180") == (-90.0, -180.0)
    assert _try_parse_latlon("90,180") == (90.0, 180.0)
