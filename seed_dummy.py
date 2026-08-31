#!/usr/bin/env python3
"""Seed or clear sample ChatMap flights."""

import argparse
import os
import random
from datetime import datetime, timedelta, timezone

import psycopg2

NEPAL = timezone(timedelta(hours=5, minutes=45))
MAP_ID = "seedmap"
CHAT_ID = os.getenv("FLIGHTS_CHAT_ID", "nepal1")
BBOX = [float(n) for n in os.getenv("FLIGHTS_BBOX", "85.0,27.5,85.9,28.3").split(",")]

MIX = [("airborne", .30), ("planned", .15), ("stale_estimate", .12),
       ("landed", .09), ("stale_no_end", .05), ("unparsed", .04)]

DB = dict(
    host=os.getenv("CHATMAP_DB_HOST", "chatmap-db"),
    port=int(os.getenv("CHATMAP_DB_PORT", "5432")),
    dbname=os.getenv("CHATMAP_DB", os.getenv("POSTGRES_DB", "chatmap")),
    user=os.getenv("CHATMAP_DB_USER", os.getenv("POSTGRES_USER", "admin")),
    password=os.getenv("CHATMAP_DB_PASSWORD", os.getenv("POSTGRES_PASSWORD", "")),
)


def rows(pilots, seed=7):
    rng = random.Random(seed)
    minlon, minlat, maxlon, maxlat = BBOX
    hhmm = lambda now, m: (now + timedelta(minutes=m)).astimezone(NEPAL).strftime("%H:%M")
    now = datetime.now(timezone.utc)

    def place(i):
        if i % 3 == 0:
            return (minlon + (maxlon - minlon) * rng.gauss(.42, .012),
                    minlat + (maxlat - minlat) * rng.gauss(.55, .012))
        return (rng.uniform(minlon, maxlon), rng.uniform(minlat, maxlat))

    def plan(now, start, length):
        return rng.choice([
            f"takeoff @ {hhmm(now, start)} landing @ {hhmm(now, start + length)}",
            f"TO {hhmm(now, start).replace(':', '')} LDG {hhmm(now, start + length).replace(':', '')}",
            f"takeoff {hhmm(now, start)} +{length}",
            f"T/O {hhmm(now, start)} landing {hhmm(now, start + length)}",
        ])

    out, i = [], 0
    for state, share in MIX:
        for _ in range(round(pilots * share)):
            i += 1
            pilot, (lon, lat) = f"seed-{i:03d}", place(i)
            if state == "airborne":
                start = rng.randint(-50, 8)
                length = max(rng.choice([45, 60, 90]), -start + rng.randint(15, 45))
                out.append((pilot, start - 5, plan(now, start, length), lon, lat, "airborne"))
            elif state == "planned":
                start = rng.randint(22, 150)
                out.append((pilot, -rng.randint(1, 20), plan(now, start, 60), lon, lat, "planned"))
            elif state == "landed":
                start = -rng.randint(60, 100)
                out.append((pilot, start - 5, plan(now, start, 55), lon, lat, "landed"))
                out.append((pilot, -rng.randint(0, 4), rng.choice(["landed", "LANDED", "down safe"]),
                            lon, lat, "landed"))
            elif state == "stale_estimate":
                start = -rng.randint(80, 200)
                out.append((pilot, start - 5, plan(now, start, 50), lon, lat, "stale"))
            elif state == "stale_no_end":
                start = -rng.randint(130, 220)
                out.append((pilot, start - 5, f"takeoff @ {hhmm(now, start)}", lon, lat, "stale"))
            else:
                out.append((pilot, -rng.randint(2, 30),
                            rng.choice(["starting work near the bridge",
                                        "flying the same area as yesterday",
                                        "up in 10"]), lon, lat, "unparsed"))

    for n, (lon, lat) in enumerate([(2.35, 48.85), (maxlon + .4, maxlat + .3)]):
        out.append((f"seed-out-{n}", -5, plan(now, -2, 60), lon, lat, "dropped"))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pilots", type=int, default=80,
                    help="number of pilots to simulate (default: 80)")
    ap.add_argument("--clear", action="store_true",
                    help="remove the sample data and exit")
    args = ap.parse_args()

    conn = psycopg2.connect(**DB)
    conn.autocommit = True
    now = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        if args.clear:
            cur.execute("DELETE FROM points WHERE map_id = %s", (MAP_ID,))
            print(f"Removed {cur.rowcount} sample points.")
            cur.execute("DELETE FROM maps WHERE id = %s", (MAP_ID,))
            return

        cur.execute("""
            INSERT INTO maps (id, name, description, sharing, owner_id,
                              created_at, updated_at, is_live)
            VALUES (%s, 'seed', '', 'PRIVATE', %s, now(), now(), true)
            ON CONFLICT (id) DO NOTHING
        """, (MAP_ID, CHAT_ID))

        data = rows(args.pilots)
        for n, (pilot, offset, message, lon, lat, _) in enumerate(data):
            cur.execute("""
                INSERT INTO points (id, geom, message, username, time, removed, map_id)
                VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s, false, %s)
                ON CONFLICT (id) DO UPDATE SET
                    geom = EXCLUDED.geom, message = EXCLUDED.message, time = EXCLUDED.time
            """, (f"seed-{n:04d}", lon, lat, message, pilot,
                  (now + timedelta(minutes=offset)).replace(tzinfo=None), MAP_ID))

    summary = {}
    for _, _, _, _, _, expect in data:
        summary[expect] = summary.get(expect, 0) + 1
    labels = {
        "dropped": "outside the flight area",
        "stale_estimate": "overdue (landing time passed)",
        "stale_no_end": "overdue (no landing time)",
        "unparsed": "unclear",
    }
    print(f"Added {len(data)} messages from {args.pilots} pilots to map {MAP_ID}.")
    print("  " + ", ".join(
        f"{count} {labels.get(status, status)}" for status, count in sorted(summary.items())
    ))
    print("  Run again with --clear to remove them.")


if __name__ == "__main__":
    main()
