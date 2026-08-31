# Nepal pilot dashboard

A live dashboard of pilot flight reports sent through WhatsApp.

Reports are ingested by [ChatMap](https://github.com/hotosm/chatmap).

> [!NOTE]
> The dashboard does not need to be public. For sensitive data, host it internally and configure `Caddyfile` for internal DNS and TLS.

## Disclaimer

**This is not an air-traffic, deconfliction, or search-and-rescue tool.** The board
shows only what pilots report over WhatsApp - a self-reported take-off pin and times,
not a live track. Reports can be missed, mistyped, delayed, or never sent, so a marker
may be wrong and an empty map does not mean empty airspace. Never use it as the sole
basis for a flight, clearance, or rescue decision.

This software is provided **“as is”, without warranty of any kind**. To the extent
permitted by law, the copyright holders and contributors are not liable for any damages
arising from its use. See the [GNU Affero General Public License](LICENSE.md) for the
complete terms.

## How it works

The pilot sends a location pin with a message such as `takeoff @ 10:30 landing @ 11:30`, then sends `landed` when safely down. Each report must be a new message; WhatsApp replies are not ingested.

- **Planned:** a light marker until 15 minutes before take-off.
- **In the air:** an active marker from 15 minutes before take-off.
- **Landed:** a muted marker after the pilot sends `landed`, then removed after a 5-minute leeway.
- **Stale:** an overdue marker after 2 hours without `landed`; old flights are cleared daily.

Click a marker for its report and copyable `lat, lon` coordinates. Click a coloured status dot to filter the map; click it again, press `Esc`, or select **show every flight again** to clear the filter.

The 15-minute active window and 2-hour stale limit are configurable. Flight data refreshes every 60 seconds.

If the feed stops, the board keeps showing the last known positions:

- **After 3 minutes** (three missed exports): an amber banner giving the time of the last update, with the map untouched.
- **After 1 hour:** a red warning banner - too old to plan against.

## Deploy

1. Point `nepal-pilots.response.hotosm.org` at the server, then copy `.env.example` to `.env` and fill it in.
2. Run `docker compose up -d`, then check that `docker compose logs chatmap-migrate` reaches `head`.
3. Run `ssh -N -L 8001:127.0.0.1:8001 user@server`, open <http://127.0.0.1:8001/start-qr?session=nepal1>, and scan the QR code once with the dedicated WhatsApp number. That phone must be in the pilots' group and **no other chat** - the linked device ingests every conversation it can see.
4. Have someone post a pin in the group, then run `docker compose exec flights uv run python /app/export_flights.py --list-chats` and put the busiest hash in `FLIGHTS_CHAT_HASH`, so only that group reaches the board.
5. Open <https://nepal-pilots.response.hotosm.org> and log in as `admin` / `password`. Set `BOARD_PASSWORD` in `.env` before real use.

## Test

Fill the database with a realistic dataset, check the board, then clear it. Do not leave seed data on the live board.

```
docker compose run --rm -v ./seed_dummy.py:/app/seed_dummy.py:ro flights \
  uv run python /app/seed_dummy.py            # --clear to remove
```

Expect 60 markers from 60 pilots: 24 airborne, 14 overdue, 12 planned, 7 landed, 3 unclear. Stop `flights` for three minutes and the board must raise the amber staleness banner while still showing every marker, and `docker compose ps` must report `flights` as `unhealthy`.

## Original specification

This is the original flight-state and refresh specification, retained for reference.

### Flight states and icons

- **Planned**
  - Before the active window: light icon.
- **In air**
  - 15 minutes before proposed take-off: set the active icon.
  - The 15-minute threshold is a tweakable parameter.
- **Landed**
  - At landing plus a 5-minute leeway: mute the icon.
  - The icon then disappears.
- **Stale**
  - No landing message after 2 hours: show the stale icon.
  - The 2-hour threshold is a tweakable parameter.

### Take-off and landing

- The pilot sends a single location point.
- The pilot writes the take-off and proposed landing time in a message, for example: `takeoff @ 10:30 landing @ 11:30`.
- The pilot sends `landed` when they actually land.
- Account for the edge case where a landing message is missing.
- Flush stale flights daily.

### Map and refresh

- Refresh every 60 seconds.
