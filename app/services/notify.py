"""Notify service: the testable core of Phase 6's background rain notifications.

The ARQ worker is a thin shell around this module. Everything here takes its
dependencies as arguments — the database session, an OTP client, a rain service and a
notifier — so the whole decision-and-outbox flow is exercised with stubs, no FastAPI and
no network (see tests/integration/test_notify.py).

The flow, per scheduler tick:

1. `due_trip_alerts` selects the trip alerts whose recurrence weekday matches *today* and
   whose departure falls inside the lead window ahead of `now`.
2. For each, `evaluate_trip_alert` plans the trip at its departure via `gather_candidates`
   and calls `recommend()` to get a recommendation (or None if the trip can't be assessed).
3. `process_trip_alert` notifies — exactly once per trip per departure day — only when
   rain is actually expected on the bike leg, recording the alert in the `notifications`
   outbox first (the row is the durable, idempotent record; delivery follows).
4. `run_due_checks` ties 1–3 together and returns the alerts actually created this tick.

All wall-clock reasoning is in Europe/Amsterdam — an Amsterdam-only service — and `now`
is passed in as an AMS-aware datetime so the worker's clock is injectable and tests are
deterministic.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.otp import OTPError
from app.models.notification import Notification
from app.models.trip_alert import TripAlert
from app.schemas.advice import AdviceResponse
from app.services.advice import recommend
from app.services.planner import gather_candidates

logger = logging.getLogger(__name__)

# Amsterdam-only service: the local timezone is a domain constant, not config (mirrors
# advice.LOCAL_TZ). `now` is expected to be aware in this zone.
AMS = ZoneInfo("Europe/Amsterdam")


async def due_trip_alerts(
    session: AsyncSession, now: datetime, lead_minutes: int
) -> list[TripAlert]:
    """Return the trip alerts due to be checked at `now`.

    An alert is due when:
    - its recurrence `days` contains today's ISO weekday (`now.isoweekday()`, 1=Mon..7=Sun),
      so it actually runs today; and
    - its `departure_time` falls within `[now.time(), (now + lead).time()]` — i.e. the trip
      departs within the next `lead_minutes`, giving the rider time to act on the alert.

    Args:
        session: the async DB session to query on.
        now: the current moment, Amsterdam-aware.
        lead_minutes: how far ahead of `now` to look for departures.

    Returns:
        The matching `TripAlert` rows (unordered).

    Midnight: when the lead window crosses 24:00 (e.g. now=23:55, lead=15 -> 23:55..00:10) it
    is split into two non-wrapping ranges so a late-night departure isn't silently missed.
    The weekday is still today's (a same-day late-night trip is the realistic case); a trip
    that recurs only on *tomorrow* and departs 00:00–00:10 is instead caught by the next
    tick's non-wrapping window, so it is never lost.
    """
    weekday = now.isoweekday()
    window_start = now.time()
    window_end = (now + timedelta(minutes=lead_minutes)).time()

    if window_end < window_start:
        # Wrapped past midnight: match [window_start, 23:59:59] OR [00:00, window_end].
        time_cond = or_(
            TripAlert.departure_time >= window_start,
            TripAlert.departure_time <= window_end,
        )
    else:
        time_cond = and_(
            TripAlert.departure_time >= window_start,
            TripAlert.departure_time <= window_end,
        )

    # `days` is a Postgres SmallInteger ARRAY; `.any(weekday)` -> "weekday = ANY(days)".
    stmt = select(TripAlert).where(TripAlert.days.any(weekday), time_cond)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def evaluate_trip_alert(
    alert: TripAlert, now: datetime, otp, rain_service
) -> AdviceResponse | None:
    """Plan `alert`'s trip at its departure and return the recommendation, or None.

    Bike routing is still mandatory: without a bike candidate there is nothing to assess
    against the rain, so we skip the alert (return None).
    """
    departure = datetime.combine(now.date(), alert.departure_time, tzinfo=AMS)
    origin = (alert.origin_lat, alert.origin_lon)
    destination = (alert.dest_lat, alert.dest_lon)

    try:
        candidates = await gather_candidates(otp, origin, destination, departure=departure)
    except OTPError:
        return None
    if not any(c.kind == "bike" for c in candidates):
        return None

    rain = await rain_service.get_forecast(lat=alert.origin_lat, lon=alert.origin_lon)
    plan = recommend(candidates, rain)
    return AdviceResponse(
        recommendation=plan.recommendation,
        reason=plan.reason,
        bike_minutes=plan.bike_minutes,
        transit_minutes=plan.transit_minutes,
        max_rain_mm_per_h=plan.max_rain_mm_per_h,
        rain_expected=plan.rain_expected,
    )


async def process_trip_alert(
    session: AsyncSession, alert: TripAlert, now: datetime, otp, rain_service, notifier
) -> Notification | None:
    """Evaluate `alert` and, only if rain is expected on the bike leg, record + deliver one alert.

    We notify strictly when `recommend()` says rain is expected for the cycling window
    (`rain_expected is True`) — not on a dry ride, and not on the degraded None case where the
    forecast was unavailable (we never warn about rain we couldn't confirm).

    Idempotency is enforced by the database, not by checking first: we INSERT into the
    `notifications` outbox with `on_conflict_do_nothing` on (trip_alert_id, departure_date).
    If a row is inserted we deliver it and return it; if the constraint already holds a row
    for this trip+day (an earlier tick today already warned), the insert no-ops and we return
    None — so re-running the 5-minute sweep can never double-notify.

    Delivery is **best-effort and single-attempt**: the outbox row is committed first (the
    durable record), then `notifier.send()` is attempted and its failures are swallowed (a
    delivery error must never abort recording or the sweep). The MVP channel is `LogNotifier`,
    which can't fail; before wiring a *fallible* channel (email/push), add a `delivered_at`
    column + per-tick re-delivery of undelivered rows so a transient outage self-heals (see
    Decisions). We do NOT re-attempt today, so a failed send is recorded-but-not-redelivered.

    Args:
        session: the async DB session (its transaction is committed on insert).
        alert: the trip alert to process.
        now: the current moment, Amsterdam-aware; `now.date()` is the departure day key.
        otp: OTP client (see `evaluate_trip_alert`).
        rain_service: rain service (see `evaluate_trip_alert`).
        notifier: delivery channel with `async send(notification)`.

    Returns:
        The created `Notification` when an alert was sent, else None (dry, unassessable, or
        already notified for this trip+day).
    """
    advice = await evaluate_trip_alert(alert, now=now, otp=otp, rain_service=rain_service)
    if advice is None or advice.rain_expected is not True:
        return None

    departure_date = now.date()
    # on_conflict_do_nothing makes the unique (trip_alert_id, departure_date) constraint the
    # idempotency guard; .returning(id) is non-empty only when this insert actually wrote a
    # row, which is exactly "we are the tick that gets to notify".
    stmt = (
        insert(Notification)
        .values(
            trip_alert_id=alert.id,
            user_id=alert.user_id,
            recommendation=advice.recommendation,
            reason=advice.reason,
            departure_date=departure_date,
        )
        .on_conflict_do_nothing(index_elements=["trip_alert_id", "departure_date"])
        .returning(Notification.id)
    )
    try:
        result = await session.execute(stmt)
        inserted_id = result.scalar_one_or_none()
        await session.commit()
    except Exception:
        # A failed execute/commit leaves asyncpg's transaction aborted; roll it back so the
        # session is usable for the next alert, then let the caller isolate this one.
        await session.rollback()
        raise

    if inserted_id is None:
        # Conflict: another tick already recorded this trip's alert for today. Don't re-send.
        return None

    notification = await session.get(Notification, inserted_id)
    try:
        await notifier.send(notification)
    except Exception:
        # Best-effort delivery: the committed row is the source of truth, so a delivery
        # failure must neither abort the sweep nor lose the record. Logged, not raised.
        logger.warning(
            "rain-alert delivery failed for trip_alert %s (recorded, not delivered)",
            alert.id,
            exc_info=True,
        )
    return notification


async def run_due_checks(
    session: AsyncSession, now: datetime, otp, rain_service, notifier, lead_minutes: int
) -> list[Notification]:
    """Process every due trip alert at `now` and return the notifications actually created.

    The single per-tick entry point the worker calls: it fans out over `due_trip_alerts` and
    collects the alerts that resulted in a fresh, delivered notification (skipping dry trips,
    unassessable ones, and trips already notified for this departure day).

    Args:
        session: the async DB session.
        now: the current moment, Amsterdam-aware.
        otp: OTP client.
        rain_service: rain service.
        notifier: delivery channel.
        lead_minutes: lead window passed to `due_trip_alerts`.

    Returns:
        The `Notification` rows created this tick (possibly empty).
    """
    alerts = await due_trip_alerts(session, now=now, lead_minutes=lead_minutes)
    created: list[Notification] = []
    for alert in alerts:
        try:
            notification = await process_trip_alert(
                session, alert, now=now, otp=otp, rain_service=rain_service, notifier=notifier
            )
        except Exception:
            # One bad alert must not starve the rest of the tick. process_trip_alert already
            # rolls back its own aborted DB transaction; here we just log and carry on so the
            # remaining due alerts are still processed.
            logger.warning("failed to process trip_alert %s; skipping", alert.id, exc_info=True)
            continue
        if notification is not None:
            created.append(notification)
    return created
