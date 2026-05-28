"""Buienradar client: parse the `raintext` feed into typed, mm/h-converted slots.

Buienradar's `raintext` endpoint returns plain text, one line per ~5-minute slot:
`iii|HH:MM`, where `iii` is a 0-255 intensity *code*, not yes/no and not mm/h. The
client's job is to parse those lines and convert each code to mm/h using Buienradar's
published formula, so the rest of the app never deals with raw codes. All external
traffic is mocked with respx — these tests run fully offline.
"""

import httpx
import respx

from app.clients.buienradar import BuienradarClient

URL = "https://gpsgadget.buienradar.nl/data/raintext"

# 077 -> 0.1 mm/h, 109 -> 1.0 mm/h (both exact under the formula); 000 -> dry.
SAMPLE = "000|16:00\n077|16:05\n109|16:10\n"


@respx.mock
async def test_parses_slots_with_time_and_intensity():
    respx.get(URL).mock(return_value=httpx.Response(200, text=SAMPLE))

    forecast = await BuienradarClient(base_url=URL).get_rain_forecast(lat=52.1, lon=5.18)

    assert len(forecast.slots) == 3
    assert [s.intensity for s in forecast.slots] == [0, 77, 109]
    assert [s.time.isoformat(timespec="minutes") for s in forecast.slots] == [
        "16:00",
        "16:05",
        "16:10",
    ]


@respx.mock
async def test_converts_intensity_code_to_mm_per_hour():
    respx.get(URL).mock(return_value=httpx.Response(200, text=SAMPLE))

    forecast = await BuienradarClient(base_url=URL).get_rain_forecast(lat=52.1, lon=5.18)

    mm = [s.mm_per_h for s in forecast.slots]
    assert mm[0] == 0.0  # code 0 means no rain, not 10**(-109/32)
    assert mm[1] == 0.1
    assert mm[2] == 1.0


@respx.mock
async def test_ignores_blank_lines_and_trailing_whitespace():
    respx.get(URL).mock(return_value=httpx.Response(200, text="000|16:00\n\n  \n077|16:05\n"))

    forecast = await BuienradarClient(base_url=URL).get_rain_forecast(lat=52.1, lon=5.18)

    assert len(forecast.slots) == 2
