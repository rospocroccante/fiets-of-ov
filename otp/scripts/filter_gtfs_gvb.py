#!/usr/bin/env python3
"""Filter the nation-wide NL GTFS feed down to a single operator (default: GVB).

The OVapi NL feed is ~200 MB and covers every Dutch operator; building an OTP graph
from all of it is slow and memory-hungry. For an Amsterdam-only trial we only need
GVB (tram/bus/metro/ferry in Amsterdam), so this script writes a small, valid
GTFS feed containing just that operator, preserving referential integrity:

    agency -> routes -> trips -> stop_times -> stops (+ parent stations)
                         |-> calendar / calendar_dates (by service_id)
                         |-> shapes (by shape_id)
    transfers / frequencies kept only where they reference surviving ids.

Stdlib only. Streams the huge stop_times.txt line by line so memory stays flat.

Usage:
    python filter_gtfs_gvb.py <input gtfs-nl.zip> <output gtfs-gvb.zip> [AGENCY_MATCH]
"""

import csv
import io
import sys
import zipfile


def _read(zf: zipfile.ZipFile, name: str):
    """Yield (fieldnames, rows-iterator) for a GTFS member, or (None, []) if absent."""
    if name not in zf.namelist():
        return None, iter(())
    raw = io.TextIOWrapper(zf.open(name), encoding="utf-8-sig", newline="")
    reader = csv.DictReader(raw)
    return reader.fieldnames, reader


def _write(out: zipfile.ZipFile, name: str, fieldnames, rows) -> int:
    """Write a GTFS member from rows (list of dicts); return the row count."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    count = 0
    for row in rows:
        writer.writerow(row)
        count += 1
    out.writestr(name, buf.getvalue())
    return count


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    match = (sys.argv[3] if len(sys.argv) > 3 else "GVB").lower()

    with zipfile.ZipFile(src) as zf:
        # 1) agency -> kept agency_ids
        fields, rows = _read(zf, "agency.txt")
        agencies = [
            r
            for r in rows
            if r.get("agency_id", "").lower() == match or match in r.get("agency_name", "").lower()
        ]
        agency_ids = {r["agency_id"] for r in agencies}
        if not agency_ids:
            sys.exit(f"no agency matched {match!r}")
        print(f"agencies: {sorted(agency_ids)}")

        # 2) routes for those agencies
        route_fields, rows = _read(zf, "routes.txt")
        routes = [r for r in rows if r.get("agency_id") in agency_ids]
        route_ids = {r["route_id"] for r in routes}
        print(f"routes: {len(route_ids)}")

        # 3) trips on those routes
        trip_fields, rows = _read(zf, "trips.txt")
        trips = [r for r in rows if r.get("route_id") in route_ids]
        trip_ids = {r["trip_id"] for r in trips}
        service_ids = {r["service_id"] for r in trips if r.get("service_id")}
        shape_ids = {r["shape_id"] for r in trips if r.get("shape_id")}
        print(f"trips: {len(trip_ids)}")

        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as out:
            _write(out, "agency.txt", fields, agencies)
            _write(out, "routes.txt", route_fields, routes)
            _write(out, "trips.txt", trip_fields, trips)

            # 4) stop_times: stream-filter, collecting referenced stops
            st_fields, rows = _read(zf, "stop_times.txt")
            stop_ids: set[str] = set()

            def _kept_stop_times():
                for r in rows:
                    if r.get("trip_id") in trip_ids:
                        stop_ids.add(r.get("stop_id"))
                        yield r

            n_st = _write(out, "stop_times.txt", st_fields, _kept_stop_times())
            print(f"stop_times: {n_st}, stops referenced: {len(stop_ids)}")

            # 5) stops + transitive parent stations
            stop_fields, rows = _read(zf, "stops.txt")
            all_stops = {r["stop_id"]: r for r in rows}
            keep = set(stop_ids)
            frontier = set(stop_ids)
            while frontier:
                parents = {
                    all_stops[s]["parent_station"]
                    for s in frontier
                    if s in all_stops and all_stops[s].get("parent_station")
                }
                parents -= keep
                keep |= parents
                frontier = parents
            _write(out, "stops.txt", stop_fields, (all_stops[s] for s in keep if s in all_stops))
            print(f"stops written: {len(keep)}")

            # 6) calendars by service_id
            for fname in ("calendar.txt", "calendar_dates.txt"):
                f, rows = _read(zf, fname)
                if f:
                    _write(out, fname, f, (r for r in rows if r.get("service_id") in service_ids))

            # 7) shapes by shape_id
            f, rows = _read(zf, "shapes.txt")
            if f:
                _write(out, "shapes.txt", f, (r for r in rows if r.get("shape_id") in shape_ids))

            # transfers / frequencies that reference surviving ids
            f, rows = _read(zf, "transfers.txt")
            if f:
                _write(
                    out,
                    "transfers.txt",
                    f,
                    (
                        r
                        for r in rows
                        if r.get("from_stop_id") in keep and r.get("to_stop_id") in keep
                    ),
                )
            f, rows = _read(zf, "frequencies.txt")
            if f:
                _write(out, "frequencies.txt", f, (r for r in rows if r.get("trip_id") in trip_ids))

            # feed_info copied verbatim if present
            f, rows = _read(zf, "feed_info.txt")
            if f:
                _write(out, "feed_info.txt", f, rows)

    print(f"wrote {dst}")


if __name__ == "__main__":
    main()
