from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.clients.buienradar import RainForecast, RainSlot
from app.clients.otp import Itinerary, Leg
from app.services.scoring import (
    Candidate,
    Weights,
    classify_kind,
    rank,
    score,
)

TZ = ZoneInfo("Europe/Amsterdam")


def ms(hour: int, minute: int) -> int:
    return int(datetime(2026, 6, 1, hour, minute, tzinfo=TZ).timestamp() * 1000)


def leg(
    mode: str,
    start: tuple[int, int],
    end: tuple[int, int],
    route: str | None = None,
    from_pt: tuple[float, float] | None = None,
    to_pt: tuple[float, float] | None = None,
) -> Leg:
    s, e = ms(*start), ms(*end)
    return Leg(
        mode=mode,
        start_time=s,
        end_time=e,
        duration=(e - s) / 1000,
        distance=1000.0,
        route_short_name=route,
        from_lat=from_pt[0] if from_pt else None,
        from_lon=from_pt[1] if from_pt else None,
        to_lat=to_pt[0] if to_pt else None,
        to_lon=to_pt[1] if to_pt else None,
    )


def itin(*legs: Leg) -> Itinerary:
    return Itinerary(
        duration=sum(leg.duration for leg in legs),
        start_time=legs[0].start_time,
        end_time=legs[-1].end_time,
        legs=list(legs),
    )


def rain_at(*wet_minutes: int) -> RainForecast:
    # mm/h 1.0 (code 109) at each given minute past 14:00, dry elsewhere across 14:00..14:55.
    slots = []
    for m in range(0, 60, 5):
        mm = 1.0 if m in wet_minutes else 0.0
        slots.append(RainSlot(time=time(14, m), intensity=109 if mm else 0, mm_per_h=mm))
    return RainForecast(slots=slots)


BIKE = itin(leg("BICYCLE", (14, 0), (14, 20)))
TRANSIT = itin(leg("WALK", (14, 0), (14, 2)), leg("TRAM", (14, 2), (14, 14), route="13"))
BIKE_RIDE = itin(
    leg("BICYCLE", (14, 0), (14, 5)),
    leg("SUBWAY", (14, 5), (14, 18), route="52"),
    leg("WALK", (14, 18), (14, 20)),
)


def test_classify_kind_by_legs():
    assert classify_kind(BIKE) == "bike"
    assert classify_kind(TRANSIT) == "transit"
    assert classify_kind(BIKE_RIDE) == "bike_and_ride"
    walk_only = itin(leg("WALK", (14, 0), (14, 30)))
    assert classify_kind(walk_only) is None


def test_classify_bike_plus_ferry_stays_bike():
    # A cyclist rolls the bike onto the GVB ferry: still a bike trip, not bike_and_ride.
    ferry_bike = itin(
        leg("BICYCLE", (14, 0), (14, 8)),
        leg("FERRY", (14, 8), (14, 14)),
        leg("BICYCLE", (14, 14), (14, 25)),
    )
    assert classify_kind(ferry_bike) == "bike"


def test_classify_bike_plus_ferry_plus_tram_is_bike_and_ride():
    mixed = itin(
        leg("BICYCLE", (14, 0), (14, 8)),
        leg("FERRY", (14, 8), (14, 14)),
        leg("TRAM", (14, 14), (14, 25), route="13"),
    )
    assert classify_kind(mixed) == "bike_and_ride"


def test_classify_ferry_without_bike_is_transit():
    ferry_walk = itin(leg("WALK", (14, 0), (14, 5)), leg("FERRY", (14, 5), (14, 15)))
    assert classify_kind(ferry_walk) == "transit"


def test_dry_prefers_bike_unless_transit_saves_a_lot():
    # Dry: bike=20 (no bias), transit=14+10 bias=24 -> bike wins.
    ordered = rank([Candidate("bike", BIKE), Candidate("transit", TRANSIT)], rain=None)
    assert [s.kind for s in ordered] == ["bike", "transit"]
    assert ordered[0].rain_minutes == 0


def test_heavy_rain_penalizes_long_exposed_bike():
    # Rain across the whole bike ride: a 20-min exposed ride is penalized far above a
    # 14-min sheltered tram, flipping the order.
    wet = rain_at(*range(0, 60, 5))  # wet every slot
    ordered = rank([Candidate("bike", BIKE), Candidate("transit", TRANSIT)], rain=wet)
    assert ordered[0].kind == "transit"
    bike_scored = next(s for s in ordered if s.kind == "bike")
    assert bike_scored.rain_minutes > 0
    assert bike_scored.cost > 20  # base 20 min + rain penalty


def test_partial_rain_favours_bike_and_ride_over_pure_bike():
    # Rain only late in the window: the short 5-min bike leg of bike-and-ride is exposed
    # far less than the 20-min pure-bike ride, so the mix wins.
    wet = rain_at(*range(10, 60, 5))  # wet from 14:10 on
    ordered = rank([Candidate("bike", BIKE), Candidate("bike_and_ride", BIKE_RIDE)], rain=wet)
    assert ordered[0].kind == "bike_and_ride"


def test_score_arithmetic_with_bike_preference():
    # transit: 14 min + 10 transit-bias, no rain, 0 transfers
    assert score(Candidate("transit", TRANSIT), rain=None).cost == 24.0
    # bike: no bias
    assert score(Candidate("bike", BIKE), rain=None).cost == 20.0


def test_transit_must_beat_bike_by_more_than_bias_to_win():
    # dry: transit saves 6 min (< 10 bias) -> bike still wins
    winner = rank([Candidate("bike", BIKE), Candidate("transit", TRANSIT)], rain=None)[0]
    assert winner.kind == "bike"


# A far-from-any-hub point used as a non-hub transfer/handoff location.
FAR = (52.30, 4.75)
CENTRAAL = (52.3791, 4.9003)


def _two_transit(transfer_pt: tuple[float, float]) -> Itinerary:
    # Two transit legs -> one transfer, at transfer_pt (the from-coords of the 2nd leg).
    return itin(
        leg("TRAM", (14, 0), (14, 10), route="13"),
        leg("BUS", (14, 12), (14, 24), route="22", from_pt=transfer_pt),
    )


def test_transfer_penalty_is_five_per_transfer():
    # _two_transit is 10 + 12 = 22 min. One transfer away from any hub:
    # 22 min + 1 * 5 transfer + 10 bias = 37.
    away = Candidate("transit", _two_transit(FAR))
    assert score(away, rain=None).cost == 22.0 + 5.0 + 10.0
    assert score(away, rain=None).cost == 37.0


def test_hub_transfer_costs_less_than_ordinary_transfer():
    # Same trip, transfer near Centraal (hub) vs. away: hub transfer is cheaper by
    # transfer_penalty_min - hub_transfer_penalty_min = 5 - 2 = 3.
    at_hub = Candidate("transit", _two_transit(CENTRAAL))
    away = Candidate("transit", _two_transit(FAR))
    assert score(away, rain=None).cost - score(at_hub, rain=None).cost == 3.0


def test_ferry_leg_gets_bonus():
    # A ferry crossing lowers cost by ferry_bonus_min (3.0) vs. the same trip without.
    with_ferry = itin(
        leg("WALK", (14, 0), (14, 2)),
        leg("FERRY", (14, 2), (14, 8), route="F3"),
    )
    without = itin(
        leg("WALK", (14, 0), (14, 2)),
        leg("BUS", (14, 2), (14, 8), route="22"),
    )
    ferry_cost = score(Candidate("transit", with_ferry), rain=None).cost
    plain_cost = score(Candidate("transit", without), rain=None).cost
    assert plain_cost - ferry_cost == 3.0


def test_station_access_bonus_for_bike_and_ride_ending_at_hub():
    # bike_and_ride whose bike leg ends at a hub scores station_access_bonus_min (2.0)
    # lower than one ending away from any hub.
    at_hub = itin(
        leg("BICYCLE", (14, 0), (14, 5), to_pt=CENTRAAL),
        leg("SUBWAY", (14, 5), (14, 18), route="52"),
    )
    away = itin(
        leg("BICYCLE", (14, 0), (14, 5), to_pt=FAR),
        leg("SUBWAY", (14, 5), (14, 18), route="52"),
    )
    hub_cost = score(Candidate("bike_and_ride", at_hub), rain=None).cost
    away_cost = score(Candidate("bike_and_ride", away), rain=None).cost
    assert away_cost - hub_cost == 2.0


def test_no_station_bonus_for_plain_bike_or_transit():
    # A plain bike itinerary ending at a hub gets no station-access bonus.
    bike_to_hub = Candidate(
        "bike", itin(leg("BICYCLE", (14, 0), (14, 20), to_pt=CENTRAAL))
    )
    assert score(bike_to_hub, rain=None).cost == 20.0
    # A transit itinerary is not eligible either.
    transit_at_hub = Candidate("transit", _two_transit(CENTRAAL))
    # 22 + 2 (hub transfer) + 10 bias = 34, no station bonus applied.
    assert score(transit_at_hub, rain=None).cost == 34.0


def test_rank_keeps_lowest_cost_candidate_per_kind():
    # Two bike candidates (differing only in duration) and one transit: rank keeps the
    # cheaper bike and the transit, best-first.
    short_bike = Candidate("bike", itin(leg("BICYCLE", (14, 0), (14, 20))))
    long_bike = Candidate("bike", itin(leg("BICYCLE", (14, 0), (14, 40))))
    transit = Candidate("transit", TRANSIT)
    ordered = rank([long_bike, short_bike, transit], rain=None)
    assert [s.kind for s in ordered] == ["bike", "transit"]
    bike = next(s for s in ordered if s.kind == "bike")
    assert bike.itinerary.duration == 20 * 60  # the cheaper (shorter) bike survived


def test_custom_weights_defaults_match_spec():
    w = Weights()
    assert w.transfer_penalty_min == 5.0
    assert w.hub_transfer_penalty_min == 2.0
    assert w.ferry_bonus_min == 3.0
    assert w.station_access_bonus_min == 2.0
    assert w.transit_bias_min == 10.0

def test_rank_shows_fastest_within_the_departure_window():
    # Product default: fastest trip wins, but only among departures within
    # max_depart_wait_min (15) of the kind's earliest option.
    now_bike = itin(leg("BICYCLE", (14, 0), (14, 38)))
    soon_bike = itin(leg("BICYCLE", (14, 10), (14, 44)))  # 34 min, departs +10: competes
    ordered = rank([Candidate("bike", now_bike), Candidate("bike", soon_bike)], rain=None)
    assert len(ordered) == 1  # one winner per kind
    assert ordered[0].itinerary is soon_bike


def test_rank_drops_departures_beyond_the_window():
    # A faster ride leaving 57 min out is no answer to "how do I go now".
    now_bike = itin(leg("BICYCLE", (14, 0), (14, 38)))
    later_bike = itin(leg("BICYCLE", (14, 57), (15, 31)))  # 34 min, departs +57: dropped
    ordered = rank([Candidate("bike", later_bike), Candidate("bike", now_bike)], rain=None)
    assert ordered[0].itinerary is now_bike


def test_departure_window_is_per_kind():
    # Transit's next service being 30 min out must not wipe transit from the options:
    # the window is measured against each kind's own earliest departure.
    late_transit = itin(
        leg("WALK", (14, 30), (14, 32)),
        leg("TRAM", (14, 32), (14, 44), route="13"),
    )
    ordered = rank([Candidate("bike", BIKE), Candidate("transit", late_transit)], rain=None)
    assert {s.kind for s in ordered} == {"bike", "transit"}


def test_depart_delay_factor_prices_waiting_within_the_window():
    # With the factor enabled, a 34-min ride leaving in 10 min costs 34+10 and loses
    # to a 38-min ride leaving now.
    now_bike = itin(leg("BICYCLE", (14, 0), (14, 38)))
    soon_bike = itin(leg("BICYCLE", (14, 10), (14, 44)))
    strict = Weights(depart_delay_factor=1.0)
    ordered = rank(
        [Candidate("bike", soon_bike), Candidate("bike", now_bike)], rain=None, weights=strict
    )
    assert ordered[0].itinerary is now_bike
