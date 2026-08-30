# Fiets of OV — API Guide

**Base URL:** `https://fiets.89.125.35.116.sslip.io/api`

Fiets of OV answers one question for a trip in Amsterdam: **bike, or public transport?**
It routes both options (OpenTripPlanner over GTFS + OSM), checks the Buienradar rain
forecast along your ride, and recommends the one that gets you there dry and fast.

All endpoints return JSON. No API key is needed for routing; only trip alerts require
an account.

---

## Quick answer: `GET /v1/advice`

The lightweight endpoint — one recommendation, no geometry. Use it when you just want
the verdict.

| Param  | Meaning                                   |
|--------|-------------------------------------------|
| `from` | Origin — a place name or `"lat,lon"`      |
| `to`   | Destination — a place name or `"lat,lon"` |

```bash
curl "https://fiets.89.125.35.116.sslip.io/api/v1/advice?from=Centraal%20Station&to=Vondelpark"
```

Real response:

```json
{
  "recommendation": "bike",
  "reason": "dry during your 17-min ride -> bike",
  "bike_minutes": 17,
  "transit_minutes": 31,
  "max_rain_mm_per_h": 0.0,
  "rain_expected": false,
  "forecast_degraded": false
}
```

- `recommendation` is `bike`, `transit`, or `bike_and_ride`.
- `reason` is human-readable and shows the deciding factor (rain intensity or time).
- `forecast_degraded: true` means Buienradar was unreachable — the advice is then
  time-only, not rain-aware. Nothing fails: you always get an answer.

## Full plan: `GET /v1/plan`

Same parameters as `/v1/advice`, but returns **every ranked option with its full
itinerary** (legs, distances, timestamps, drawable geometry) — this is what the web
app renders on the map.

```bash
curl "https://fiets.89.125.35.116.sslip.io/api/v1/plan?from=Centraal%20Station&to=Vondelpark"
```

Response shape (geometry trimmed):

```json
{
  "recommendation": "bike",
  "reason": "dry during your 17-min ride -> bike",
  "max_rain_mm_per_h": 0.0,
  "rain_expected": false,
  "forecast_degraded": false,
  "origin":      { "name": "Centraal Station", "lat": 52.3791, "lon": 4.9003 },
  "destination": { "name": "Vondelpark",       "lat": 52.3579, "lon": 4.8686 },
  "options": [
    {
      "kind": "bike",
      "recommended": true,
      "score": ...,
      "rain_minutes": 0,
      "itinerary": { "minutes": 17, "distance_m": ..., "start_time": ..., "end_time": ..., "legs": [ ... ] }
    },
    { "kind": "transit", "recommended": false, ... }
  ]
}
```

## Nearby stops: `GET /v1/stops`

GVB stops around a point, nearest first.

| Param    | Meaning                                  |
|----------|-------------------------------------------|
| `lat`    | Latitude (−90..90)                        |
| `lon`    | Longitude (−180..180)                     |
| `radius` | Search radius in metres (1..5000, default 500) |

```bash
curl "https://fiets.89.125.35.116.sslip.io/api/v1/stops?lat=52.3791&lon=4.9003&radius=300"
```

Real response (trimmed):

```json
[
  {
    "stop_id": "stoparea:615089",
    "code": "Centraal Station",
    "name": "Centraal Station",
    "lat": 52.37899993749999,
    "lon": 4.90092375,
    "location_type": 1,
    "distance_m": 43.8
  },
  { "stop_id": "3979877", "name": "Amsterdam, Centraal Station", "distance_m": 67.2, ... }
]
```

## Health: `GET /health`

```bash
curl "https://fiets.89.125.35.116.sslip.io/api/health"
# {"status":"ok"}
```

---

## Accounts & rain alerts

A trip alert is a recurring trip (e.g. your commute) that the service watches: it
geocodes your endpoints once, then checks the forecast around your departure and can
tell you *before you leave* whether today is a bike day.

### 1. Register — `POST /v1/auth/register`

```bash
curl -X POST "https://fiets.89.125.35.116.sslip.io/api/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "password": "at-least-8-chars"}'
```

→ `201 {"id": 1, "email": "you@example.com"}` · duplicate email → `409`.

### 2. Log in — `POST /v1/auth/token` (OAuth2 password form; email goes in `username`)

```bash
curl -X POST "https://fiets.89.125.35.116.sslip.io/api/v1/auth/token" \
  -d "username=you@example.com&password=at-least-8-chars"
```

→ `{"access_token": "<JWT>", "token_type": "bearer"}`

### 3. Create an alert — `POST /v1/trip-alerts` (Bearer token required)

`days` are ISO weekdays (Mon=1 .. Sun=7); `departure_time` is naive local `HH:MM`.

```bash
curl -X POST "https://fiets.89.125.35.116.sslip.io/api/v1/trip-alerts" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"label": "Commute", "from": null, "origin": "Centraal Station",
       "destination": "Vondelpark", "departure_time": "08:30", "days": [1,2,3,4,5]}'
```

List them with `GET /v1/trip-alerts` (same header).

---

## Errors — what they mean for you

| Code | Meaning |
|------|---------|
| 400  | A place could not be geocoded — check the spelling or pass `"lat,lon"` |
| 401  | Missing/invalid token (auth endpoints never reveal whether an email exists) |
| 404  | `no route found for this trip` — the trip itself is impossible, not an outage |
| 409  | Email already registered |
| 422  | Invalid body (blank fields, bad weekday, timezone-aware departure_time) |
| 502  | Routing upstream (OTP) unavailable — retry shortly |

## Good to know

- **Place strings are free-form.** `from=Dam` works; so does `from=52.373,4.893`.
  Geocoding is biased to the Amsterdam area.
- **The first request after a quiet period can take a few seconds** — the service
  scales to zero and wakes on demand.
- **Responses are cached briefly** for identical routing queries, so polling the same
  trip is cheap.
