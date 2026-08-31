# Nepal pilot dashboard

In short: drone pilots report flights in WhatsApp, and ATC watches them on a live map.  
The points come from [ChatMap](https://github.com/hotosm/chatmap) and are self-reported, so do not treat them as verified.

## How it works

The pilot sends one location pin with a message such as `takeoff @ 10:30 landing @ 11:30`, then sends `landed` when safely down. Pilots must send a new message rather than replying to an existing one, as WhatsApp replies are not ingested.

- **Planned:** a light marker until 15 minutes before take-off.
- **In the air:** an active marker from 15 minutes before take-off.
- **Landed:** a muted marker after the pilot sends `landed`, then removed after a 5-minute leeway.
- **Stale:** an overdue marker after 2 hours without `landed`; old flights are cleared daily.

The 15-minute active window and 2-hour stale limit are configurable. Flight data refreshes every 60 seconds.

If the feed stops, the board stays up and keeps the last known positions on the map:

- **After 3 minutes** (three missed exports): an amber banner giving the time of the last update, with the map untouched.
- **After 1 hour:** a red banner and the map greys out - too old to plan against.

## Deploy

1. Point `nepal-pilots.response.hotosm.org` at the server, then copy `.env.example` to `.env` and fill it in.
2. Run `docker compose up -d`, then check that `docker compose logs chatmap-migrate` reaches `head`.
3. Run `ssh -N -L 8001:127.0.0.1:8001 user@server`, open <http://127.0.0.1:8001/start-qr?session=nepal1>, and scan the QR code once with the dedicated WhatsApp number. That phone must be in the pilots' group and **no other chat** - the linked device ingests every conversation it can see.
4. Have someone post a pin in the group, then run `docker compose exec flights uv run python /app/export_flights.py --list-chats` and put the busiest hash in `FLIGHTS_CHAT_HASH`, so only that group reaches the board.
5. Open <https://nepal-pilots.response.hotosm.org> and log in as `admin` / `password`. Set `BOARD_PASSWORD` in `.env` before real use.

## Test

Fill the database with a realistic morning (80 pilots, every icon state), check the board, then clear it. Never leave seed data on a live board.

```
docker compose run --rm -v ./seed_dummy.py:/app/seed_dummy.py:ro flights \
  uv run python /app/seed_dummy.py            # --clear to remove
```

Expect 60 markers: 24 airborne, 14 overdue, 12 planned, 7 landed, 3 unclear. Stop `flights` for three minutes and the board must raise the amber staleness banner while still showing every marker.
