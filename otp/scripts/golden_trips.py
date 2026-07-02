#!/usr/bin/env python3
"""Golden bike trips: routing-quality benchmark against a live OTP.

Runs a fixed set of bike-only trips spanning Amsterdam and its edges through the
OTP GraphQL API, printing distance, duration, and detour ratio (route distance /
straight-line distance) per trip. Run it before and after a graph or parameter
change and compare the outputs.

Usage:
    python otp/scripts/golden_trips.py [--otp http://localhost:8080]

Exit code 0 when every trip returned at least one bike itinerary, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the repo root importable: the benchmark must send exactly the query and
# parameters the app sends, so it reuses the client's constants instead of
# duplicating values that would silently drift.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

from app.clients.otp import (
    _BIKE_RELUCTANCE,
    _BIKE_SPEED,
    _BIKE_TRIANGLE,
    _NUM_ITINERARIES,
    _PLAN_QUERY,
    _SEARCH_WINDOW_S,
    _TRANSFER_PENALTY,
    _WALK_BOARD_COST,
    _WALK_RELUCTANCE,
    _WALK_SPEED,
    BIKE_MODES,
    _to_transport_modes,
)
from app.services.geo import haversine_m

# (name, from_lat, from_lon, to_lat, to_lon) — spans the city plus the edge areas
# that the old BBBike extract clipped (Amstelveen, Diemen).
TRIPS: tuple[tuple[str, float, float, float, float], ...] = (
    ("Centraal -> Bijlmer ArenA", 52.3791, 4.9003, 52.3122, 4.9471),
    ("NDSM -> Osdorpplein", 52.4010, 4.8935, 52.3585, 4.7900),
    ("Centraal -> Amstelveen Stadshart", 52.3791, 4.9003, 52.3010, 4.8630),
    ("Diemen Sniep -> Vondelpark", 52.3390, 4.9700, 52.3580, 4.8686),
    ("Zuid -> Sloterdijk", 52.3389, 4.8730, 52.3889, 4.8375),
    ("IJburg -> Centraal", 52.3560, 5.0010, 52.3791, 4.9003),
    ("De Pijp -> Noorderpark", 52.3556, 4.8926, 52.3920, 4.9190),
    ("Science Park -> RAI", 52.3540, 4.9540, 52.3376, 4.8896),
)


def plan_bike(
    client: httpx.Client,
    otp: str,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
) -> dict:
    """Return the first bike itinerary, raising RuntimeError when OTP has none."""
    variables = {
        "from": {"lat": from_lat, "lon": from_lon},
        "to": {"lat": to_lat, "lon": to_lon},
        "modes": _to_transport_modes(BIKE_MODES),
        "num": _NUM_ITINERARIES,
        "date": None,
        "time": None,
        "searchWindow": _SEARCH_WINDOW_S,
        "walkReluctance": _WALK_RELUCTANCE,
        "walkSpeed": _WALK_SPEED,
        "bikeSpeed": _BIKE_SPEED,
        "bikeReluctance": _BIKE_RELUCTANCE,
        "transferPenalty": _TRANSFER_PENALTY,
        "walkBoardCost": _WALK_BOARD_COST,
        "optimize": "TRIANGLE",
        "triangle": _BIKE_TRIANGLE,
    }
    response = client.post(
        f"{otp}/otp/gtfs/v1", json={"query": _PLAN_QUERY, "variables": variables}
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    itineraries = ((data.get("data") or {}).get("plan") or {}).get("itineraries") or []
    if not itineraries:
        raise RuntimeError("no itineraries")
    return itineraries[0]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--otp", default="http://localhost:8080", help="OTP base URL")
    args = parser.parse_args()
    otp = args.otp.rstrip("/")

    print(f"bikeSpeed={_BIKE_SPEED} triangle={_BIKE_TRIANGLE}")
    print(f"{'trip':<34} {'km':>7} {'min':>6} {'detour':>7}  modes")
    failures = 0
    with httpx.Client(timeout=30.0) as client:
        for name, from_lat, from_lon, to_lat, to_lon in TRIPS:
            try:
                itin = plan_bike(client, otp, from_lat, from_lon, to_lat, to_lon)
            except (httpx.HTTPError, RuntimeError) as exc:
                failures += 1
                print(f"{name:<34} FAILED: {exc}")
                continue
            distance_m = sum(leg.get("distance") or 0.0 for leg in itin["legs"])
            minutes = itin["duration"] / 60
            crow_m = haversine_m(from_lat, from_lon, to_lat, to_lon)
            detour = distance_m / crow_m if crow_m else float("nan")
            leg_modes = ",".join(dict.fromkeys(leg["mode"] for leg in itin["legs"]))
            print(
                f"{name:<34} {distance_m / 1000:>7.2f} {minutes:>6.1f} {detour:>7.2f}  {leg_modes}"
            )
    if failures:
        print(f"{failures}/{len(TRIPS)} trips failed", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
