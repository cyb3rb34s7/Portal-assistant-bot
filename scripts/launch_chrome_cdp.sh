#!/usr/bin/env bash
# Launches Chrome with CDP enabled on port 9222 using a dedicated profile.
# The profile is isolated so it never interferes with your daily browsing.
#
# On first run, open http://localhost:5173 in this Chrome window and sign in
# to any auth you need. The pilot will drive THIS window via CDP.

set -euo pipefail

CDP_PORT="${CDP_PORT:-9222}"
PROFILE_DIR="${PROFILE_DIR:-$HOME/.curationpilot-chrome-profile}"

find_chrome() {
  if command -v google-chrome >/dev/null 2>&1; then
    echo "google-chrome"; return
  fi
  if command -v google-chrome-stable >/dev/null 2>&1; then
    echo "google-chrome-stable"; return
  fi
  if command -v chromium >/dev/null 2>&1; then
    echo "chromium"; return
  fi
  if [[ "$(uname -s)" == "Darwin" ]]; then
    local mac_chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if [[ -x "$mac_chrome" ]]; then
      echo "$mac_chrome"; return
    fi
  fi
  echo "" 
}

CHROME_BIN="$(find_chrome)"
if [[ -z "$CHROME_BIN" ]]; then
  echo "Could not locate Chrome. Set CHROME_BIN manually." >&2
  exit 1
fi

mkdir -p "$PROFILE_DIR"

echo "Launching Chrome with CDP on port $CDP_PORT"
echo "Profile: $PROFILE_DIR"
echo "Close this Chrome window when you are done."

"$CHROME_BIN" \
  --remote-debugging-port="$CDP_PORT" \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run \
  --no-default-browser-check \
  "http://localhost:5173"
