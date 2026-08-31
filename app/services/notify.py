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
   outbox first (the row is the durable, idempotent record; delivery follows), then
   stamping `delivered_at` once the send succeeds.
4. `redeliver_pending` retries rows whose delivery failed on an earlier tick.
5. `run_due_checks` ties 1–4 together and returns the alerts actually created this tick.

All wall-clock reasoning is in Europe/Amsterdam — an Amsterdam-only service — and `now`
is passed in as an AMS-aware datetime so the worker's clock is injectable and tests are
deterministic.
"""

import asyncio
import logging
from contextlib import nullcontext
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, update
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

# Ceiling on redelivery attempts per tick. The retry set is already bounded by "still
# departing today", so this only guards the pathological case — a channel that has been down
# all day — from spending a whole 5-minute tick on retries and starving the fresh sweep.
MAX_REDELIVERIES_PER_TICK = 50


async def due_trip_alerts(
    session: AsyncSession, now: datetime, lead_minutes: int
) -> list[TripAlert]:
    """Return the trip alerts due to be checked at `now`.

    An alert is due when its `departure_time` falls within the next `lead_minutes`
    (giving the rider time to act) *and* its recurrence `days` contains the ISO weekday
    (1=Mon..7=Sun) of the day that departure actually lands on — today, or tomorrow for
    the post-midnight tail of a wrapped window.

    Args:
        session: the async DB session to query on.
        now: the current moment, Amsterdam-aware.
        lead_minutes: how far ahead of `now` to look for departures.

    Returns:
        The matching `TripAlert` rows (unordered).

    Midnight: when the lead window crosses 24:00 (e.g. now=23:55, lead=15 -> 23:55..00:10)
    it is split into two non-wrapping ranges, each matched against its own weekday: the
    pre-midnight part against *today*, the post-midnight part against *tomorrow*. A 00:05
    departure seen from Monday 23:55 belongs to a Tuesday recurrence — matching it against
    Monday would both fire Monday-only alerts a day early (for a departure 23h50m in the
    past) and skip Tuesday-only ones entirely.
    """
    weekday = now.isoweekday()
    window_start = now.time()
    window_end = (now + timedelta(minutes=lead_minutes)).time()

    # `days` is a Postgres SmallInteger ARRAY; `.any(weekday)` -> "weekday = ANY(days)".
    if window_end < window_start:
        # Wrapped past midnight: [window_start, 23:59:59] belongs to today's recurrence,
        # [00:00, window_end] to tomorrow's — each portion carries its own weekday, so a
        # Monday-only 00:05 alert is due Sunday night, not Monday night.
        tomorrow = (now + timedelta(days=1)).isoweekday()
        due_cond = or_(
            and_(TripAlert.days.any(weekday), TripAlert.departure_time >= window_start),
            and_(TripAlert.days.any(tomorrow), TripAlert.departure_time <= window_end),
        )
    else:
        due_cond = and_(
            TripAlert.days.any(weekday),
            TripAlert.departure_time >= window_start,
            TripAlert.departure_time <= window_end,
        )

    stmt = select(TripAlert).where(due_cond)
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _next_departure(alert: TripAlert, now: datetime) -> datetime:
    """The departure moment `alert` refers to at `now`.

    Today at `departure_time` — or tomorrow when that time has already passed, which is
    exactly the post-midnight tail a wrapped lead window selects (see `due_trip_alerts`):
    at Monday 23:55 a 00:05 alert means *Tuesday* 00:05, not a moment 23h50m in the past.
    """
    date = now.date()
    if alert.departure_time < now.time():
        date += timedelta(days=1)
    return datetime.combine(date, alert.departure_time, tzinfo=AMS)


async def evaluate_trip_alert(
    alert: TripAlert, now: datetime, otp, rain_service
) -> AdviceResponse | None:
    """Plan `alert`'s trip at its departure and return the recommendation, or None.

    Bike routing is still mandatory: without a bike candidate there is nothing to assess
    against the rain, so we skip the alert (return None).
    """
    departure = _next_departure(alert, now)
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
        forecast_degraded=plan.forecast_degraded,
    )


async def process_trip_alert(
    session: AsyncSession,
    alert: TripAlert,
    now: datetime,
    otp,
    rain_service,
    notifier,
    db_lock: asyncio.Lock | None = None,
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

    Delivery is **at-least-once**: the outbox row is committed first (the durable record),
    then `notifier.send()` is attempted, and only a success stamps `delivered_at`. A failure
    is logged and swallowed — it must never abort recording or the sweep — but the row is
    left undelivered, so `redeliver_pending` picks it up on the next tick and a transient
    outage in a fallible channel (email/push) self-heals. The duplicate risk that "at least
    once" implies is the right side to err on for a rain warning: a second copy is an
    annoyance, a missing one defeats the feature.

    Args:
        session: the async DB session (its transaction is committed on insert).
        alert: the trip alert to process.
        now: the current moment, Amsterdam-aware. The departure day key is today — or
            tomorrow for a post-midnight departure caught by a wrapped lead window.
        otp: OTP client (see `evaluate_trip_alert`).
        rain_service: rain service (see `evaluate_trip_alert`).
        notifier: delivery channel with `async send(notification)`.
        db_lock: serializes the DB work when the sweep runs alerts concurrently — the
            shared `AsyncSession` must never execute two statements at once. None (the
            serial callers) skips locking entirely.

    Returns:
        The created `Notification` when an alert was sent, else None (dry, unassessable, or
        already notified for this trip+day).
    """
    advice = await evaluate_trip_alert(alert, now=now, otp=otp, rain_service=rain_service)
    if advice is None:
        return None
    if advice.forecast_degraded:
        # "We couldn't check" is not "it's dry". The rain fields are all None here and the
        # guard below would drop the alert exactly as it drops a genuinely dry trip, so say
        # so out loud: an operator seeing a quiet worker during a Buienradar outage should be
        # able to find the reason in the log rather than assume Amsterdam had a nice day.
        logger.info(
            "trip_alert %s not assessed: no rain forecast available (not notifying)", alert.id
        )
        return None
    if advice.rain_expected is not True:
        return None

    departure_date = _next_departure(alert, now).date()
    # on_conflict_do_nothing makes the unique (trip_alert_id, departure_date) constraint the
    # idempotency guard; .returning(Notification) hands back the full inserted row in the
    # same round trip (no follow-up SELECT) and is non-empty only when this insert actually
    # wrote a row, which is exactly "we are the tick that gets to notify".
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
        .returning(Notification)
    )
    # Everything that touches the session (execute/commit/rollback) sits inside the lock;
    # the slow network evaluation above deliberately does not, so concurrent alerts still
    # overlap where it matters.
    async with db_lock if db_lock is not None else nullcontext():
        try:
            result = await session.execute(stmt)
            notification = result.scalars().one_or_none()
            await session.commit()
        except Exception:
            # A failed execute/commit leaves asyncpg's transaction aborted; roll it back so
            # the session is usable for the next alert, then let the caller isolate this one.
            await session.rollback()
            raise

    if notification is None:
        # Conflict: another tick already recorded this trip's alert for today. Don't re-send.
        return None

    await deliver(session, notification, notifier, db_lock=db_lock)
    return notification


async def deliver(
    session: AsyncSession,
    notification: Notification,
    notifier,
    db_lock: asyncio.Lock | None = None,
) -> bool:
    """Attempt delivery of an already-recorded `notification`, stamping it on success.

    The single place a send is attempted, so recording and redelivery cannot drift apart in
    how they interpret success. A raised exception from the channel is logged and swallowed
    (returning False): the committed row is the source of truth, and a delivery failure must
    never abort the caller's sweep. Leaving `delivered_at` NULL is precisely what schedules
    the retry — see `redeliver_pending`.

    The stamp is `func.now()`, so the timestamp comes from the database's clock like
    `created_at` does, rather than from whichever worker happened to run the tick.

    Args:
        session: the async DB session; committed here when the stamp is written.
        notification: the persisted outbox row to deliver.
        notifier: delivery channel with `async send(notification)`.
        db_lock: as in `process_trip_alert` — held only around the DB work, never around
            the (slow, network-bound) send.

    Returns:
        True when the channel accepted the alert and the row was stamped, else False.
    """
    try:
        await notifier.send(notification)
    except Exception:
        logger.warning(
            "rain-alert delivery failed for trip_alert %s (recorded, will retry next tick)",
            notification.trip_alert_id,
            exc_info=True,
        )
        return False

    async with db_lock if db_lock is not None else nullcontext():
        try:
            # Written as an explicit UPDATE rather than by mutating the ORM object: the row
            # came back from an `insert(...).returning(...)`, and an UPDATE by primary key
            # makes the write independent of whether that instance is attached to this
            # session — the same statement then serves redelivery, which loads rows normally.
            await session.execute(
                update(Notification)
                .where(Notification.id == notification.id)
                .values(delivered_at=func.now())
            )
            await session.commit()
        except Exception:
            # The user *has* been notified; only the bookkeeping failed. Roll back so the
            # session stays usable, and let the row's NULL cause one extra delivery on a
            # later tick — a duplicate alert beats an aborted sweep.
            await session.rollback()
            logger.warning(
                "could not stamp delivered_at for notification %s; it may be re-delivered",
                notification.id,
                exc_info=True,
            )
            return False
    return True


async def redeliver_pending(
    session: AsyncSession,
    now: datetime,
    notifier,
    limit: int = MAX_REDELIVERIES_PER_TICK,
) -> list[Notification]:
    """Re-attempt outbox rows recorded earlier but never confirmed delivered.

    This is what makes the outbox worth having: without it a channel that blips for one tick
    leaves a row asserting an alert the user never received. Each tick we pick up the NULL
    `delivered_at` rows and try again, so a transient outage costs a few minutes of delay
    rather than a lost warning.

    The retry set is bounded by relevance, not by an attempt counter: only alerts whose
    `departure_date` is still today or later are retried, because a rain warning for a trip
    that already left is noise. That also means a permanently broken channel cannot build an
    ever-growing backlog — yesterday's failures simply age out, still on record as
    undelivered for anyone auditing.

    Args:
        session: the async DB session.
        now: the current moment, Amsterdam-aware; its date is the relevance cutoff.
        notifier: delivery channel with `async send(notification)`.
        limit: ceiling on rows attempted this tick, so retries can't starve the fresh sweep.

    Returns:
        The notifications successfully delivered on this pass (possibly empty).
    """
    stmt = (
        select(Notification)
        .where(Notification.delivered_at.is_(None), Notification.departure_date >= now.date())
        # Oldest first: the alert closest to being useless gets its retry first.
        .order_by(Notification.created_at)
        .limit(limit)
    )
    result = await session.execute(stmt)
    pending = list(result.scalars().all())

    delivered: list[Notification] = []
    for notification in pending:
        # Serial, and no lock: this runs before the concurrent sweep starts, so it owns the
        # session outright. Retries are also the path most likely to hit a channel that is
        # still down, and hammering it in parallel would help nobody.
        if await deliver(session, notification, notifier):
            delivered.append(notification)
    if delivered:
        logger.info("re-delivered %s previously undelivered rain alert(s)", len(delivered))
    return delivered


async def run_due_checks(
    session: AsyncSession, now: datetime, otp, rain_service, notifier, lead_minutes: int
) -> list[Notification]:
    """Process every due trip alert at `now` and return the notifications actually created.

    The single per-tick entry point the worker calls: it fans out over `due_trip_alerts` and
    collects the alerts that resulted in a fresh, delivered notification (skipping dry trips,
    unassessable ones, and trips already notified for this departure day).

    Each tick opens with `redeliver_pending`, retrying alerts a previous tick recorded but
    failed to send. It runs *first*, and deliberately so: rows left over from an earlier tick
    have had time for the channel to recover, whereas anything that fails during this tick's
    own sweep would be retried seconds later against a channel we just watched break. Its
    failures never block the sweep — a still-down channel logs and moves on.

    Alerts are processed with bounded concurrency: an alert's cost is dominated by its
    OTP + rain fan-out, so overlapping up to 4 keeps a large sweep inside one scheduler
    tick while the semaphore stops a burst of due alerts from stampeding OTP. The shared
    `AsyncSession` cannot run overlapping statements, so all DB work stays serialized
    behind one lock (see `process_trip_alert`). Per-alert error isolation is unchanged:
    one bad alert is logged and skipped, never aborting the rest of the tick.

    Args:
        session: the async DB session.
        now: the current moment, Amsterdam-aware.
        otp: OTP client.
        rain_service: rain service.
        notifier: delivery channel.
        lead_minutes: lead window passed to `due_trip_alerts`.

    Returns:
        The `Notification` rows created *this* tick (possibly empty). Rows merely
        re-delivered are not included — they were already reported when they were created.
    """
    try:
        await redeliver_pending(session, now=now, notifier=notifier)
    except Exception:
        # Retrying old alerts is strictly a bonus pass; if it blows up (a wedged session, a
        # channel raising something exotic) the tick's real work must still happen. Roll back
        # first: a failed statement leaves asyncpg's transaction aborted, and every query in
        # the sweep below would then fail too — turning a skippable extra into a lost tick.
        await session.rollback()
        logger.warning("redelivery pass failed; continuing with the due-check sweep", exc_info=True)

    alerts = await due_trip_alerts(session, now=now, lead_minutes=lead_minutes)

    semaphore = asyncio.Semaphore(4)
    db_lock = asyncio.Lock()

    async def _process(alert: TripAlert) -> Notification | None:
        async with semaphore:
            try:
                return await process_trip_alert(
                    session,
                    alert,
                    now=now,
                    otp=otp,
                    rain_service=rain_service,
                    notifier=notifier,
                    db_lock=db_lock,
                )
            except Exception:
                # One bad alert must not starve the rest of the tick. process_trip_alert
                # already rolls back its own aborted DB transaction; log and carry on so
                # the remaining due alerts are still processed.
                logger.warning("failed to process trip_alert %s; skipping", alert.id, exc_info=True)
                return None

    results = await asyncio.gather(*(_process(alert) for alert in alerts))
    return [n for n in results if n is not None]
