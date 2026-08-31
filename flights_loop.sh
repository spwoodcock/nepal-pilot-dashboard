#!/bin/sh
while :; do
  uv run python /app/export_flights.py --out /out/flights.geojson || true
  sleep "${FLIGHTS_INTERVAL:-60}"
done
