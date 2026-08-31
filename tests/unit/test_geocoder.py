"""Geocoder client: turn an Amsterdam place name into coordinates via Nominatim.

Nominatim is an untrusted upstream like the others: every call is timed out, bounded
to the Amsterdam area, and sends the User-Agent its usage policy requires. A query that
matches nothing raises `GeocodeNotFound` (a 400-class input problem), while transport or
HTTP failures propagate as `httpx.HTTPError` (a 502-class upstream problem). The HTTP GET
is mocked with respx so these run offline.
"""

import httpx
import pytest
import respx

from app.clients.geocoder import GeocodeNotFound, GeocoderClient, GeocoderError

URL = "https://nominatim.test/search"
UA = "fiets-of-ov-test/0.1"


def _result(lat: str, lon: str) -> list[dict]:
    # Nominatim returns lat/lon as strings in a list, newest-match-first.
    return [{"lat": lat, "lon": lon, "display_name": "somewhere in Amsterdam"}]


@respx.mock
async def test_geocode_returns_lat_lon():
    respx.get(URL).mock(return_value=httpx.Response(200, json=_result("52.3791", "4.9003")))

    coords = await GeocoderClient(base_url=URL, user_agent=UA).geocode("Amsterdam Centraal")

    assert coords == (52.3791, 4.9003)


@respx.mock
async def test_geocode_query_is_bounded_to_amsterdam_with_user_agent():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=_result("52.36", "4.88")))

    await GeocoderClient(base_url=URL, user_agent=UA).geocode("Vondelpark")

    request = route.calls.last.request
    params = httpx.URL(str(request.url)).params
    assert params["q"] == "Vondelpark"
    assert params["bounded"] == "1"
    assert params["countrycodes"] == "nl"
    assert params["limit"] == "1"
    assert "viewbox" in params  # restricts the search to the Amsterdam bbox
    # Nominatim's usage policy requires a valid, identifying User-Agent.
    assert request.headers["user-agent"] == UA


@respx.mock
async def test_geocode_raises_not_found_on_empty_result():
    respx.get(URL).mock(return_value=httpx.Response(200, json=[]))

    with pytest.raises(GeocodeNotFound):
        await GeocoderClient(base_url=URL, user_agent=UA).geocode("Nowhere Lane 999")


@respx.mock
async def test_geocode_raises_http_error_on_upstream_failure():
    respx.get(URL).mock(return_value=httpx.Response(503))

    with pytest.raises(httpx.HTTPError):
        await GeocoderClient(base_url=URL, user_agent=UA).geocode("Amsterdam Centraal")


@respx.mock
async def test_geocode_caches_repeat_queries():
    # Place names don't move; caching repeats keeps us within Nominatim's ~1 req/s policy.
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=_result("52.36", "4.88")))
    client = GeocoderClient(base_url=URL, user_agent=UA)

    first = await client.geocode("Dam")
    second = await client.geocode("dam")  # same place, different casing

    assert first == second
    assert route.calls.call_count == 1  # second answer came from the cache


@respx.mock
async def test_geocode_cache_is_bounded():
    # The per-process name cache must not grow without limit on a long-lived worker:
    # past the bound the oldest entries are evicted first.
    respx.get(URL).mock(return_value=httpx.Response(200, json=_result("52.36", "4.88")))
    client = GeocoderClient(base_url=URL, user_agent=UA)

    for i in range(600):
        await client.geocode(f"place {i}")

    assert len(client._cache) <= 512
    assert "place 0" not in client._cache  # the oldest entry was evicted
    assert "place 599" in client._cache  # the newest is still cached


@respx.mock
async def test_geocode_raises_geocoder_error_on_non_json_200():
    # A 2xx whose body isn't JSON (proxy error page, truncated response) is an upstream
    # fault: it must surface as GeocoderError (502-class), not a bare ValueError (500).
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>gateway error</html>"))

    with pytest.raises(GeocoderError):
        await GeocoderClient(base_url=URL, user_agent=UA).geocode("Amsterdam Centraal")


@respx.mock
async def test_geocode_raises_geocoder_error_on_malformed_result():
    # Same for a JSON body that isn't the documented shape (missing lat/lon, wrong types).
    respx.get(URL).mock(
        return_value=httpx.Response(200, json=[{"display_name": "no coordinates here"}])
    )

    with pytest.raises(GeocoderError):
        await GeocoderClient(base_url=URL, user_agent=UA).geocode("Amsterdam Centraal")
