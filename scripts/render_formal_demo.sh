#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CAST="${1:-$ROOT/docs/clawmonitor-formal-demo-20260321.cast}"
BASE="${CAST%.cast}"
SVG="${BASE}.svg"
GIF="${BASE}.gif"
MP4="${BASE}.mp4"

if [[ ! -f "$CAST" ]]; then
  echo "cast not found: $CAST" >&2
  exit 1
fi

echo "[render] svg-term -> $SVG"
svg-term \
  --in "$CAST" \
  --out "$SVG" \
  --window \
  --width 140 \
  --height 40

echo "[render] agg -> $GIF"
agg \
  --theme asciinema \
  --speed 1.15 \
  --idle-time-limit 1.0 \
  --cols 140 \
  --rows 40 \
  "$CAST" "$GIF"

echo "[render] ffmpeg -> $MP4"
ffmpeg -y \
  -i "$GIF" \
  -movflags faststart \
  -pix_fmt yuv420p \
  -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=lanczos" \
  "$MP4"

echo "[render] done"
ls -lh "$SVG" "$GIF" "$MP4"
