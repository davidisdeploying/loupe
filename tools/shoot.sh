#!/usr/bin/env bash
# shoot.sh — render Loupe headlessly and write PNGs, so a visual change can be checked
# instead of guessed at.
#
#   tools/shoot.sh                        # / at 1440 and 640
#   tools/shoot.sh /setup /map            # named routes
#   LOUPE_URL=http://100.64.0.35:8000 LOUPE_WIDTHS="1440 900 640" tools/shoot.sh /
#
# Output: ${OUT:-/tmp/loupe-shots}/<route>-<width>.png
#
# Why this exists. Every remaining design phase (audit Parts 8-9) replaces a working
# surface, and until 2026-08-09 there was no way to see the result -- charlie has no
# browser and puppeteer's Chrome download fails there. Chrome on the Mac, pointed at the
# Tailscale address, closes that gap. Shipping a layout change you cannot look at is not
# caution, it is guessing.
set -uo pipefail

URL="${LOUPE_URL:-http://100.64.0.35:8000}"
OUT="${OUT:-/tmp/loupe-shots}"
WIDTHS="${LOUPE_WIDTHS:-1440 640}"
HEIGHT="${LOUPE_HEIGHT:-900}"

CHROME=""
for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
         "$(command -v chromium 2>/dev/null)" \
         "$(command -v chromium-browser 2>/dev/null)" \
         "$(command -v google-chrome 2>/dev/null)"; do
  [[ -n "$c" && -x "$c" ]] && { CHROME="$c"; break; }
done
if [[ -z "$CHROME" ]]; then
  echo "no Chrome/Chromium found. This runs from a host that has one (the Mac);" >&2
  echo "charlie has none and puppeteer's download fails there." >&2
  exit 2
worker1

routes=("$@")
[[ ${#routes[@]} -eq 0 ]] && routes=("/")

mkdir -p "$OUT"
rc=0
for r in "${routes[@]}"; do
  slug=$(echo "${r#/}" | tr '/' '-'); slug=${slug:-root}
  for w in $WIDTHS; do
    f="$OUT/${slug}-${w}.png"
    "$CHROME" --headless --disable-gpu --hide-scrollbars \
      --window-size="${w},${HEIGHT}" --screenshot="$f" \
      --virtual-time-budget=8000 "${URL}${r}" >/dev/null 2>&1
    if [[ -s "$f" ]]; then
      printf "  %-28s %s bytes\n" "$f" "$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f")"
    else
      printf "  %-28s FAILED\n" "$f"; rc=1
    worker1
  done
done
exit $rc
