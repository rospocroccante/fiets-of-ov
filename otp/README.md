# Self-hosted OTP (Phase 8 trial)

Run OpenTripPlanner locally so the app routes Amsterdam trips without any third-party
planner. This is an **optional** overlay — the core app talks to whatever `OTP_BASE_URL`
points at.

## Inputs (not committed — see `.gitignore`)

Place these in `otp/data/`:

| File | Source |
| --- | --- |
| `amsterdam.osm.pbf` | https://download.bbbike.org/osm/bbbike/Amsterdam/Amsterdam.osm.pbf |
| `gtfs-gvb.zip` | NL feed `https://gtfs.ovapi.nl/nl/gtfs-nl.zip`, filtered to GVB (below) |
| `otp-2.6.0-shaded.jar` | Maven Central (only needed for the native, non-Docker run) |

Filter the 200 MB national GTFS down to GVB (Amsterdam) — a few MB, much faster to build:

```bash
python otp/scripts/filter_gtfs_gvb.py otp/data/gtfs-nl.zip otp/data/gtfs-gvb.zip GVB
```

## Run

### Docker (recommended)

```bash
docker compose -f docker-compose.yml -f docker-compose.otp.yml up
```

First start builds `otp/data/graph.obj` (a few minutes), then serves on `:8080`.

### Native (Java 21, no Docker)

```bash
otp/scripts/run_otp.sh           # builds graph if missing, then serves on :8080
```

## Point the app at it

```
OTP_BASE_URL=http://localhost:8080
```

The OTP client posts to the OTP2 **GTFS GraphQL** API at `${OTP_BASE_URL}/otp/gtfs/v1`.

## Notes

- Graph build memory: `-Xmx8g` is comfortable for a GVB+Amsterdam graph (tune via
  `OTP_HEAP` for the native script, or `JAVA_TOOL_OPTIONS` for Docker).
- Coverage is GVB within the Amsterdam OSM extract. To widen it, use a larger OSM
  extract and/or keep more operators when filtering the GTFS.
- Changes to `router-config.json` (e.g. the accessEgress stop-count cap) take effect on
  OTP server restart — no graph rebuild needed.
