#!/usr/bin/env python3
"""Export current ChatMap flight reports as GeoJSON."""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone


def env(*names, default):
    return next((os.environ[n] for n in names if os.environ.get(n)), default)


NEPAL = timezone(timedelta(hours=5, minutes=45))

PRE_WINDOW = timedelta(minutes=int(env("FLIGHTS_PRE_WINDOW_MIN", default="15")))
POST_WINDOW = timedelta(minutes=int(env("FLIGHTS_POST_WINDOW_MIN", default="5")))
STALE_AFTER = timedelta(minutes=int(env("FLIGHTS_STALE_AFTER_MIN", default="120")))
DROP_STALE_AFTER = timedelta(hours=4)  # Past battery life; prevents old flights accumulating.
LOOKBACK = timedelta(hours=14)  # One operating day.

BBOX = [float(n) for n in
        env("FLIGHTS_BBOX", default="84.479370,27.474161,86.044922,28.623104").split(",")]

NOTE_CHARS = 120
ID_KEY = env("FLIGHTS_ID_KEY", default="")
OUT_PATH = "/out/flights.geojson"

# ChatMap drops chat IDs, so restrict output to senders observed in the pilot chat.
# Use --list-chats to find its hash.
CHAT_HASH = env("FLIGHTS_CHAT_HASH", default="")
ALLOWLIST = "/out/senders.json"

REDIS_HOST = env("REDIS_HOST", default="chatmap-redis")
REDIS_PORT = int(env("REDIS_PORT", default="6379"))
ENC_KEY = env("CHATMAP_ENC_KEY", default="")

DB = {
    "host": env("CHATMAP_DB_HOST", default="chatmap-db"),
    "port": int(env("CHATMAP_DB_PORT", default="5432")),
    "dbname": env("CHATMAP_DB", "POSTGRES_DB", default="chatmap"),
    "user": env("CHATMAP_DB_USER", "POSTGRES_USER", default="admin"),
    "password": env("CHATMAP_DB_PASSWORD", "POSTGRES_PASSWORD", default=""),
}

SQL = """
SELECT p.username, p.message, p.time,
       ST_X(p.geom) AS lon, ST_Y(p.geom) AS lat
FROM points p
JOIN maps m ON m.id = p.map_id
WHERE NOT p.removed AND p.geom IS NOT NULL AND m.is_live
  AND m.owner_id = %(chat_id)s
  AND p.time > (now() AT TIME ZONE 'UTC') - make_interval(mins => %(lookback)s)
ORDER BY p.time
"""

TIME = r"([0-2]?\d)\s*[:.h]?\s*([0-5]\d)"
TAKEOFF_RE = re.compile(
    r"\b(?:take\s*-?\s*off|takeoff|t/?o|dep(?:art(?:ing|ure)?)?)\b\W{0,4}" + TIME,
    re.I)
LANDING_RE = re.compile(
    r"\b(?:land(?:ing|ed|s)?|ldg|l/?d|rtb|down)\b\W{0,4}" + TIME, re.I)
LANDED_NOW_RE = re.compile(r"\b(?:landed|land(?:ing|s)?|ldg|rtb|down\s+safe)\b", re.I)
TAKEOFF_NOW_RE = re.compile(
    r"\b(?:taking\s*off|takeoff|took\s*off|airborne|in\s+the\s+air|launching|"
    r"flying\s+now|up\s+now)\b", re.I)
DURATION_RE = re.compile(r"(?:\+|\bfor\b)\s*(\d{1,3})\s*(?:m|min|mins|minutes)?\b",
                         re.I)


def pilot_id(sender_hash):
    """Re-key a sender hash before publication."""
    return hmac.new(ID_KEY.encode(), sender_hash.encode(),
                    hashlib.sha256).hexdigest()[:8]


def resolve_clock(hour, minute, near):
    """Resolve a reported time to the nearest local date."""
    if hour > 23 or minute > 59:
        return None
    local = near.astimezone(NEPAL)
    candidates = [
        datetime.combine(local.date() + timedelta(days=offset),
                         local.time().replace(hour=hour, minute=minute,
                                              second=0, microsecond=0),
                         tzinfo=NEPAL)
        for offset in (-1, 0, 1)
    ]
    return min(candidates, key=lambda c: abs(c - near)).astimezone(timezone.utc)


def parse_report(message, sent_at):
    text = message or ""
    out = {"takeoff": None, "landing": None, "takeoff_now": False}

    match = TAKEOFF_RE.search(text)
    if match:
        out["takeoff"] = resolve_clock(int(match.group(1)), int(match.group(2)), sent_at)

    match = LANDING_RE.search(text)
    if match:
        out["landing"] = resolve_clock(int(match.group(1)), int(match.group(2)), sent_at)

    if out["takeoff"] and not out["landing"]:
        match = DURATION_RE.search(text)
        if match:
            out["landing"] = out["takeoff"] + timedelta(minutes=int(match.group(1)))

    if out["takeoff"] and out["landing"] and out["landing"] < out["takeoff"]:
        out["landing"] += timedelta(days=1)

    if not out["landing"] and LANDED_NOW_RE.search(text):
        out["landing"] = sent_at
    if not out["takeoff"] and not out["landing"] and TAKEOFF_NOW_RE.search(text):
        out["takeoff"], out["takeoff_now"] = sent_at, True

    # A landing reported alone is confirmation; one in a flight plan is an estimate.
    out["landing_confirmed"] = bool(out["landing"]) and not out["takeoff"]
    out["parsed"] = bool(out["takeoff"] or out["landing"])
    return out


def merge(reports):
    reports.sort(key=lambda r: r["sent_at"])
    flight = {"takeoff": None, "landing": None, "lon": None, "lat": None,
              "landing_confirmed": False,
              "sent_at": reports[-1]["sent_at"], "note": reports[-1]["message"],
              "parsed": any(r["parsed"] for r in reports)}

    for report in reports:
        if report["lon"] is not None:
            flight["lon"], flight["lat"] = report["lon"], report["lat"]

        if report["takeoff_now"]:
            flight["takeoff"] = report["sent_at"]
        elif report["takeoff"]:
            flight["takeoff"], flight["landing"] = report["takeoff"], report["landing"]
            flight["landing_confirmed"] = False

        if report["landing"]:
            flight["landing"] = report["landing"]
            flight["landing_confirmed"] |= report["landing_confirmed"]

    return flight


def status_of(flight, now):
    takeoff, landing = flight["takeoff"], flight["landing"]
    if landing and flight["landing_confirmed"] and now >= landing + POST_WINDOW:
        return None
    if not flight["parsed"]:
        return "unparsed" if now < flight["sent_at"] + STALE_AFTER else None
    if takeoff and now > takeoff + DROP_STALE_AFTER:
        return None
    if landing and now >= landing + POST_WINDOW:
        return "stale"
    if landing and now >= landing:
        return "landed"
    if takeoff and now < takeoff - PRE_WINDOW:
        return "planned"
    if takeoff and not landing and now > takeoff + STALE_AFTER:
        return "stale"
    return "airborne"


def report(sender, sent_at, message, lon=None, lat=None):
    return {"sender": sender, "sent_at": sent_at, "message": message,
            "lon": lon, "lat": lat, **parse_report(message, sent_at)}


def fetch_points(chat_id):
    import psycopg2
    from psycopg2.extras import RealDictCursor

    conn = psycopg2.connect(**DB)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(SQL, {"chat_id": chat_id,
                              "lookback": int(LOOKBACK.total_seconds() // 60)})
            rows = cur.fetchall()
    finally:
        conn.close()

    return [report(row["username"], row["time"].replace(tzinfo=timezone.utc),
                   row["message"], row["lon"], row["lat"]) for row in rows]


def stream_entries(session):
    import redis
    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=5)
    for _id, raw in client.xrange(f"messages:{session}", min="-", max="+"):
        yield {k.decode(): v.decode() for k, v in raw.items()}


def fetch_messages(chat_id):
    from Crypto.Cipher import AES

    out, in_chat = [], set()
    for fields in stream_entries(chat_id):
        if CHAT_HASH and fields.get("chat") == CHAT_HASH:
            in_chat.add(fields["from"])
        if not fields.get("text"):
            continue
        blob = base64.b64decode(fields["text"])
        nonce, ciphertext = blob[:12], blob[12:]
        text = AES.new(ENC_KEY.encode(), AES.MODE_GCM, nonce=nonce) \
                  .decrypt_and_verify(ciphertext[:-16], ciphertext[-16:]).decode()
        sent_at = datetime.fromisoformat(fields["date"]).astimezone(timezone.utc)
        out.append(report(fields["from"], sent_at, text))
    return out, in_chat


def gather(chat_id):
    reports = fetch_points(chat_id)
    seen = {(r["sender"], r["sent_at"].replace(second=0, microsecond=0),
             (r["message"] or "").strip()) for r in reports}

    in_chat, stream_ok = set(), False
    try:
        extras, in_chat = fetch_messages(chat_id)
        stream_ok = True
        for extra in extras:
            key = (extra["sender"], extra["sent_at"].replace(second=0, microsecond=0),
                   (extra["message"] or "").strip())
            if key not in seen:
                reports.append(extra)
                seen.add(key)
    except Exception as err:
        print(f"Warning: could not read follow-up messages ({err}). "
              f"Pilots must send their pin again with 'landed'.", file=sys.stderr)

    # Filter only after reading the stream; an empty board could imply an empty sky.
    if CHAT_HASH and stream_ok:
        allowed = set(json.load(open(ALLOWLIST))) if os.path.exists(ALLOWLIST) else set()
        allowed |= in_chat  # Persist senders because Redis retains only 30 minutes.
        json.dump(sorted(allowed), open(ALLOWLIST, "w"))
        dropped = [r for r in reports if r["sender"] not in allowed]
        if dropped:
            print(f"{len(dropped)} report(s) from outside the pilots' group ignored",
                  file=sys.stderr)
        reports = [r for r in reports if r["sender"] in allowed]
    elif CHAT_HASH:
        print("Warning: message stream unreadable, so reports from other chats "
              "cannot be filtered out this run.", file=sys.stderr)

    return reports


def build(reports, now):
    minlon, minlat, maxlon, maxlat = BBOX
    fenced = 0
    by_pilot = defaultdict(list)
    for item in reports:
        if item["lon"] is not None and not (minlon <= item["lon"] <= maxlon
                                            and minlat <= item["lat"] <= maxlat):
            fenced += 1
            continue
        by_pilot[item["sender"]].append(item)

    features, pilots = [], []
    for sender, pilot_reports in sorted(by_pilot.items()):
        flight = merge(pilot_reports)
        status = status_of(flight, now)
        props = {
            "pilot": pilot_id(sender),
            "status": status,
            "takeoff": flight["takeoff"].isoformat() if flight["takeoff"] else None,
            "landing": flight["landing"].isoformat() if flight["landing"] else None,
            "reported_at": flight["sent_at"].isoformat(),
            "landing_confirmed": flight["landing_confirmed"],
        }

        pilots.append({
            **props,
            "reports": len(pilot_reports),
            "first_report": min(r["sent_at"] for r in pilot_reports).isoformat(),
            "located": flight["lon"] is not None,
        })

        if flight["lon"] is None or not status:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [flight["lon"], flight["lat"]]},
            "properties": {**props,
                           "note": (flight["note"] or "")[:NOTE_CHARS]},
        })

    return features, pilots, fenced


def snapshot(chat_id):
    now = datetime.now(timezone.utc)
    features, pilots, fenced = build(gather(chat_id), now)
    counts = defaultdict(int)
    for feature in features:
        counts[feature["properties"]["status"]] += 1

    return {
        "type": "FeatureCollection",
        "properties": {
            "generated_at": now.isoformat(),
            "counts": dict(counts),
            "pilots": pilots,
            "window_hours": round(LOOKBACK.total_seconds() / 3600, 1),
            "thresholds": {
                "pre_window_min": int(PRE_WINDOW.total_seconds() // 60),
                "post_window_min": int(POST_WINDOW.total_seconds() // 60),
                "stale_after_min": int(STALE_AFTER.total_seconds() // 60),
                "drop_stale_after_min": int(DROP_STALE_AFTER.total_seconds() // 60),
            },
            "bbox": BBOX,
        },
        "features": features,
    }, fenced


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chat-id", "--chat_id", dest="chat_id",
                    default=env("FLIGHTS_CHAT_ID", default=""),
                    help="linked-device session ID (or set FLIGHTS_CHAT_ID)")
    ap.add_argument("--list-chats", action="store_true",
                    help="show chat hashes seen on the stream, to set FLIGHTS_CHAT_HASH")
    ap.add_argument("--out", default=OUT_PATH,
                    help=f"output file (default: {OUT_PATH}; use - for stdout)")
    args = ap.parse_args()

    if args.list_chats:
        seen = defaultdict(lambda: [0, set()])
        for fields in stream_entries(args.chat_id):
            row = seen[fields.get("chat", "?")]
            row[0] += 1
            row[1].add(fields.get("from"))
        for chat, (n, senders) in sorted(seen.items(), key=lambda kv: -kv[1][0]):
            print(f"{chat}  {n:4} messages  {len(senders):3} senders")
        return

    if not args.chat_id:
        ap.error("set --chat-id or FLIGHTS_CHAT_ID")
    if not ID_KEY:
        ap.error("set FLIGHTS_ID_KEY (generate one with: openssl rand -hex 32)")

    payload, fenced = snapshot(args.chat_id)
    counts = payload["properties"]["counts"]
    body = json.dumps(payload, indent=1).encode()

    if args.out == "-":
        sys.stdout.write(body.decode())
    else:
        # Publish atomically so the dashboard never reads a partial file.
        tmp = f"{args.out}.tmp"
        with open(tmp, "wb") as handle:
            handle.write(body)
        os.replace(tmp, args.out)

    labels = {"airborne": "in the air", "stale": "overdue", "unparsed": "unclear"}
    summary = ", ".join(
        f"{n} {labels.get(status, status)}" for status, n in sorted(counts.items())
    ) or "no current flights"
    window = payload["properties"]["window_hours"]
    pilots = len(payload["properties"]["pilots"])
    print(f"{summary}; {pilots} pilot(s) in the last {window:g}h -> {args.out}"
          + (f" ({fenced} outside the flight area)" if fenced else ""))


if __name__ == "__main__":
    main()
