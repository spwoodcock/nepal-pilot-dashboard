#!/bin/sh
# Export loop for the live board. Kept in a file rather than inline in
# compose.yml, as the syntax is tricky.
while :; do
  uv run python /app/export_flights.py --out /out/flights.geojson || true
  sleep "${FLIGHTS_INTERVAL:-60}"
done
