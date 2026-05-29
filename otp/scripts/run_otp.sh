#!/usr/bin/env bash
# Build (if needed) and run a local self-hosted OTP for Amsterdam.
#
# Prereqs: Java 21, and these files present in otp/data/ (downloaded separately):
#   otp-2.6.0-shaded.jar, amsterdam.osm.pbf, gtfs-gvb.zip
# The graph is built once into otp/data/graph.obj, then reused on subsequent runs.
set -euo pipefail

OTP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$OTP_DIR/data"
JAR="$DATA/otp-2.6.0-shaded.jar"
PORT="${OTP_PORT:-8080}"
HEAP="${OTP_HEAP:-8g}"

# OTP reads build-config.json / router-config.json from the build directory.
cp "$OTP_DIR/build-config.json" "$DATA/build-config.json"
cp "$OTP_DIR/router-config.json" "$DATA/router-config.json"

if [ ! -f "$DATA/graph.obj" ]; then
  echo "Building graph (this takes a few minutes)..."
  java "-Xmx$HEAP" -jar "$JAR" --build --save "$DATA"
fi

echo "Starting OTP on http://localhost:$PORT (GraphQL at /otp/gtfs/v1)"
exec java "-Xmx$HEAP" -jar "$JAR" --load "$DATA" --port "$PORT"
