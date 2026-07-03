"""Integration tests for the notify service (Phase 6 — the testable core).

These exercise `app/services/notify.py` against a real, migrated Postgres so the actual
`notifications` outbox table, its (trip_alert_id, departure_date) UNIQUE constraint, and the
`on_conflict_do_nothing` insert are all proven end-to-end — the idempotency guard is a
database constraint, so it can only be tested for real against Postgres.

Everything *outside* the database is stubbed:
- a stub OTP client returns a fixed bike itinerary (and a fixed transit plan), so no HTTP;
- a stub rain service returns a hand-built forecast, so the decision is deterministic;
- a capturing notifier records the `send()` calls, so we can assert delivery without I/O.

The OTP/rain fixtures mirror `tests/unit/test_advice.py`: the bike ride window (14:00–14:20
on a fixed CEST day) overlaps a wet Buienradar slot, so `decide()` yields
`rain_expected=True` and the service must notify. The trip alert's `departure_time` is 14:00,
and `now` is anchored just before it, so the alert falls inside the lead window.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.clients.buienradar import RainForecast, RainSlot
from app.clients.otp import Itinerary, Leg, Plan
from app.models.notification import Notification
from app.models.trip_alert import TripAlert
from app.models.user import User
from app.services.notify import (
    due_trip_alerts,
    evaluate_trip_alert,
    process_trip_alert,
    run_due_checks,
)

AMS = ZoneInfo("Europe/Amsterdam")

# A fixed CEST day so the epoch-ms itinerary timestamps line up with the wall-clock rain
# slots (same anchor as the advice unit tests).
DAY = (2026, 6, 1)


def _ms(hour: int, minute: int) -> int:
    """Epoch milliseconds for a local time-of-day on the fixed CEST day."""
    return int(datetime(*DAY, hour, minute, tzinfo=AMS).timestamp() * 1000)


def _bike_itinerary(start=(14, 0), end=(14, 20)) -> Itinerary:
    """A bike itinerary whose ride window is `start`–`end` on the fixed day."""
    duration_min = (end[0] * 60 + end[1]) - (start[0] * 60 + start[1])
    return Itinerary(
        duration=duration_min * 60,
        start_time=_ms(*start),
        end_time=_ms(*end),
        legs=[
            Leg(
                mode="BICYCLE",
                start_time=_ms(*start),
                end_time=_ms(*end),
                duration=duration_min * 60,
                distance=3000.0,
            )
        ],
    )


def _tram_plan() -> Plan:
    """A transit plan with one usable itinerary: short walk, then tram 13."""
    return Plan(
        itineraries=[
            Itinerary(
                duration=720,
                start_time=_ms(14, 0),
                end_time=_ms(14, 12),
                legs=[
                    Leg(
                        mode="WALK",
                        start_time=_ms(14, 0),
                        end_time=_ms(14, 2),
                        duration=120,
                        distance=150.0,
                    ),
                    Leg(
                        mode="TRAM",
                        start_time=_ms(14, 2),
                        end_time=_ms(14, 12),
                        duration=600,
                        distance=2400.0,
                        route_short_name="13",
                    ),
                ],
            )
        ]
    )


def _wet_forecast() -> RainForecast:
    """A forecast with rain (1.2 mm/h) at 14:05, inside the 14:00–14:20 ride window."""
    return RainForecast(
        slots=[
            RainSlot(time=time(14, 0), intensity=0, mm_per_h=0.0),
            RainSlot(time=time(14, 5), intensity=200, mm_per_h=1.2),
            RainSlot(time=time(14, 10), intensity=120, mm_per_h=0.6),
        ]
    )


def _dry_forecast() -> RainForecast:
    """A forecast that stays dry through the ride window (rain only later, at 15:00)."""
    return RainForecast(
        slots=[
            RainSlot(time=time(14, 0), intensity=0, mm_per_h=0.0),
            RainSlot(time=time(14, 10), intensity=0, mm_per_h=0.0),
            RainSlot(time=time(14, 20), intensity=0, mm_per_h=0.0),
            RainSlot(time=time(15, 0), intensity=200, mm_per_h=1.0),
        ]
    )


class _StubOTP:
    """Stand-in for OTPClient.plan: returns fixed itineraries for the 3-query fan-out.

    The planner issues three parallel queries (BICYCLE / TRANSIT,WALK /
    BICYCLE,TRANSIT,WALK); bike_minutes comes from the BICYCLE query result.
    The from/to coordinates are ignored, but every call's (mode, departure) is recorded
    so tests can assert the service plans at the trip's departure time.
    `bike=None` simulates "no bike itinerary found"; `raise_for_origin` makes the
    BICYCLE plan raise a non-OTPError for one origin (to test mid-sweep error isolation).
    """

    def __init__(
        self,
        *,
        bike: Itinerary | None,
        transit: Plan | None,
        raise_for_origin: tuple[float, float] | None = None,
    ) -> None:
        self._bike = bike
        self._transit = transit
        self._raise_for_origin = raise_for_origin
        self.calls: list[tuple[str, object]] = []

    async def plan(
        self, *, from_place, to_place, mode, departure=None, num_itineraries=None, slim=False
    ) -> Plan:
        self.calls.append((mode, departure))
        if mode == "BICYCLE":
            if self._raise_for_origin is not None and from_place == self._raise_for_origin:
                raise RuntimeError("boom (non-OTPError from a malformed response)")
            return Plan(itineraries=[self._bike] if self._bike is not None else [])
        return self._transit if self._transit is not None else Plan(itineraries=[])


class _StubRain:
    """Stand-in for RainService.get_forecast: returns a fixed forecast for any location."""

    def __init__(self, forecast: RainForecast | None) -> None:
        self._forecast = forecast
        self.calls: list[tuple[float, float]] = []

    async def get_forecast(self, *, lat: float, lon: float) -> RainForecast | None:
        self.calls.append((lat, lon))
        return self._forecast


class _CapturingNotifier:
    """A notifier that records each delivered notification instead of sending it anywhere."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)


async def _make_user(session, email: str = "owner@example.com") -> User:
    """Insert a user and return it (the trip alert needs an owner FK)."""
    user = User(email=email, hashed_password="x")
    session.add(user)
    await session.flush()
    return user


async def _make_alert(
    session,
    user: User,
    *,
    departure_time: time = time(14, 0),
    days: list[int] | None = None,
    origin_lat: float = 52.379,
    origin_lon: float = 4.900,
) -> TripAlert:
    """Insert a trip alert for `user` and return it.

    Defaults to a Monday (ISO 1) trip departing 14:00 from a fixed origin/destination, which
    the fixed-day fixtures (14:00–14:20 ride) line up with. The origin can be overridden so a
    test can give two alerts distinct origins (e.g. to fail OTP for only one of them).
    """
    alert = TripAlert(
        user_id=user.id,
        label="commute",
        origin="A",
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        destination="B",
        dest_lat=52.337,
        dest_lon=4.885,
        departure_time=departure_time,
        days=days if days is not None else [1],
    )
    session.add(alert)
    await session.flush()
    return alert


# `now` is anchored to a Monday at 13:55, 5 minutes before the 14:00 departures, so a default
# alert sits inside a 15-minute lead window. 2026-06-01 is a Monday (ISO weekday 1).
NOW = datetime(*DAY, 13, 55, tzinfo=AMS)


async def test_due_trip_alerts_filters_by_weekday_and_time(session):
    # (a) Seed three alerts and assert only the one matching BOTH weekday and the lead window
    # is returned. `now` is Monday 13:55, lead 15 min -> window covers [13:55, 14:10].
    user = await _make_user(session)
    due = await _make_alert(session, user, departure_time=time(14, 0), days=[1])
    wrong_weekday = await _make_alert(session, user, departure_time=time(14, 0), days=[2])
    wrong_time = await _make_alert(session, user, departure_time=time(20, 0), days=[1])

    result = await due_trip_alerts(session, now=NOW, lead_minutes=15)

    ids = {a.id for a in result}
    assert due.id in ids
    assert wrong_weekday.id not in ids  # Tuesday-only alert, but `now` is Monday
    assert wrong_time.id not in ids  # 20:00 departure is outside the [13:55, 14:10] window


async def test_process_trip_alert_with_rain_creates_one_notification(session):
    # (b) Rain on the bike leg -> exactly one Notification row, and the notifier is called.
    user = await _make_user(session)
    alert = await _make_alert(session, user)
    otp = _StubOTP(bike=_bike_itinerary(), transit=_tram_plan())
    rain = _StubRain(_wet_forecast())
    notifier = _CapturingNotifier()

    created = await process_trip_alert(
        session, alert, now=NOW, otp=otp, rain_service=rain, notifier=notifier
    )

    assert created is not None
    assert created.trip_alert_id == alert.id
    assert created.user_id == user.id
    assert created.departure_date == NOW.date()
    assert created.recommendation == "transit"  # rain -> take the tram

    count = await session.scalar(
        select(func.count()).select_from(Notification).where(Notification.trip_alert_id == alert.id)
    )
    assert count == 1
    assert len(notifier.sent) == 1
    assert notifier.sent[0].id == created.id


async def test_process_trip_alert_is_idempotent_per_departure_day(session):
    # (c) A second run for the same alert+departure_date must NOT create a second row.
    user = await _make_user(session)
    alert = await _make_alert(session, user)
    otp = _StubOTP(bike=_bike_itinerary(), transit=_tram_plan())
    rain = _StubRain(_wet_forecast())
    notifier = _CapturingNotifier()

    first = await process_trip_alert(
        session, alert, now=NOW, otp=otp, rain_service=rain, notifier=notifier
    )
    second = await process_trip_alert(
        session, alert, now=NOW, otp=otp, rain_service=rain, notifier=notifier
    )

    assert first is not None
    assert second is None  # conflict on (trip_alert_id, departure_date) -> nothing inserted
    count = await session.scalar(
        select(func.count()).select_from(Notification).where(Notification.trip_alert_id == alert.id)
    )
    assert count == 1  # still exactly one row
    assert len(notifier.sent) == 1  # the notifier was NOT called again


async def test_process_trip_alert_no_rain_creates_nothing(session):
    # (d) Dry ride -> advice.rain_expected is False -> no Notification, notifier not called.
    user = await _make_user(session)
    alert = await _make_alert(session, user)
    otp = _StubOTP(bike=_bike_itinerary(), transit=_tram_plan())
    rain = _StubRain(_dry_forecast())
    notifier = _CapturingNotifier()

    created = await process_trip_alert(
        session, alert, now=NOW, otp=otp, rain_service=rain, notifier=notifier
    )

    assert created is None
    count = await session.scalar(
        select(func.count()).select_from(Notification).where(Notification.trip_alert_id == alert.id)
    )
    assert count == 0
    assert notifier.sent == []


async def test_evaluate_trip_alert_returns_none_without_bike_itinerary(session):
    # A trip we can't assess (OTP returned no bike itinerary) must be skipped, not crash.
    user = await _make_user(session)
    alert = await _make_alert(session, user)
    otp = _StubOTP(bike=None, transit=_tram_plan())
    rain = _StubRain(_wet_forecast())

    advice = await evaluate_trip_alert(alert, now=NOW, otp=otp, rain_service=rain)

    assert advice is None


async def test_run_due_checks_end_to_end_notifies_on_rain(session):
    # (e) End-to-end: one due alert with rain -> run_due_checks returns one notification and
    # the notifier was called once.
    user = await _make_user(session)
    alert = await _make_alert(session, user)
    otp = _StubOTP(bike=_bike_itinerary(), transit=_tram_plan())
    rain = _StubRain(_wet_forecast())
    notifier = _CapturingNotifier()

    created = await run_due_checks(
        session, now=NOW, otp=otp, rain_service=rain, notifier=notifier, lead_minutes=15
    )

    assert len(created) == 1
    assert created[0].trip_alert_id == alert.id
    assert len(notifier.sent) == 1


async def test_evaluate_plans_at_the_trip_departure_time(session):
    # The headline reason Phase 6 touched the OTP client: the trip must be planned at its
    # departure, not "now". Assert the BICYCLE plan received the alert's departure datetime.
    user = await _make_user(session)
    alert = await _make_alert(session, user, departure_time=time(14, 0))
    otp = _StubOTP(bike=_bike_itinerary(), transit=_tram_plan())

    await evaluate_trip_alert(alert, now=NOW, otp=otp, rain_service=_StubRain(_wet_forecast()))

    expected = datetime.combine(NOW.date(), time(14, 0), tzinfo=AMS)
    bike_calls = [dep for mode, dep in otp.calls if mode == "BICYCLE"]
    assert bike_calls == [expected]


async def test_process_with_no_rain_data_creates_nothing(session):
    # Forecast unavailable -> decide() returns rain_expected=None. We must NOT notify about
    # rain we couldn't confirm (guard is `is not True`, not `is False`).
    user = await _make_user(session)
    alert = await _make_alert(session, user)
    otp = _StubOTP(bike=_bike_itinerary(), transit=_tram_plan())
    notifier = _CapturingNotifier()

    created = await process_trip_alert(
        session, alert, now=NOW, otp=otp, rain_service=_StubRain(None), notifier=notifier
    )

    assert created is None
    count = await session.scalar(
        select(func.count()).select_from(Notification).where(Notification.trip_alert_id == alert.id)
    )
    assert count == 0
    assert notifier.sent == []


async def test_run_due_checks_isolates_a_failing_alert(session):
    # One alert whose OTP plan raises a NON-OTPError must not abort the sweep: the other due
    # rainy alert still gets exactly one notification, and run_due_checks does not raise.
    user = await _make_user(session)
    bad = await _make_alert(session, user, origin_lat=52.111, origin_lon=4.111)
    good = await _make_alert(session, user, origin_lat=52.379, origin_lon=4.900)
    otp = _StubOTP(bike=_bike_itinerary(), transit=_tram_plan(), raise_for_origin=(52.111, 4.111))
    notifier = _CapturingNotifier()

    created = await run_due_checks(
        session,
        now=NOW,
        otp=otp,
        rain_service=_StubRain(_wet_forecast()),
        notifier=notifier,
        lead_minutes=15,
    )

    assert {n.trip_alert_id for n in created} == {good.id}  # only the healthy alert
    assert bad.id not in {n.trip_alert_id for n in created}
    assert len(notifier.sent) == 1


async def test_delivery_failure_does_not_abort_or_lose_the_record(session):
    # If the notifier raises, the row is still recorded (the outbox is the source of truth)
    # and the exception must not propagate (a delivery error can't crash the sweep).
    class _FailingNotifier:
        async def send(self, notification) -> None:
            raise RuntimeError("delivery channel down")

    user = await _make_user(session)
    alert = await _make_alert(session, user)
    otp = _StubOTP(bike=_bike_itinerary(), transit=_tram_plan())

    created = await process_trip_alert(
        session,
        alert,
        now=NOW,
        otp=otp,
        rain_service=_StubRain(_wet_forecast()),
        notifier=_FailingNotifier(),
    )

    assert created is not None  # recorded despite the delivery failure
    count = await session.scalar(
        select(func.count()).select_from(Notification).where(Notification.trip_alert_id == alert.id)
    )
    assert count == 1


async def test_due_trip_alerts_handles_a_midnight_wrapping_window(session):
    # now=23:55 with lead 15 -> window 23:55..00:10 wraps past midnight. A trip departing
    # 23:58 today must still be found (the naive between-comparison would miss it).
    late_now = datetime(*DAY, 23, 55, tzinfo=AMS)  # 2026-06-01 is a Monday (ISO 1)
    user = await _make_user(session)
    alert = await _make_alert(session, user, departure_time=time(23, 58), days=[1])

    result = await due_trip_alerts(session, now=late_now, lead_minutes=15)

    assert alert.id in {a.id for a in result}
