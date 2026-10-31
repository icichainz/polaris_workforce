#!/usr/bin/env sh
# Build the office dashboard CSS with the Tailwind v4 standalone CLI.
#
#   scripts/build_css.sh           # one-off, minified build -> static/css/app.css
#   scripts/build_css.sh --watch   # rebuild on every template change (development)
#
# The binary is looked up in this order:
#   1. $TAILWINDCSS (path to the binary)
#   2. ~/.local/bin/tailwindcss
#   3. tailwindcss on PATH
# Download it from https://github.com/tailwindlabs/tailwindcss/releases (v4.x,
# e.g. tailwindcss-macos-arm64), make it executable and put it in one of those places.
# Never commit the binary.
set -eu

cd "$(dirname "$0")/.."

if [ -n "${TAILWINDCSS:-}" ]; then
  bin="$TAILWINDCSS"
elif [ -x "$HOME/.local/bin/tailwindcss" ]; then
  bin="$HOME/.local/bin/tailwindcss"
elif command -v tailwindcss >/dev/null 2>&1; then
  bin="$(command -v tailwindcss)"
else
  echo "tailwindcss not found. Set TAILWINDCSS or install the v4 standalone CLI." >&2
  exit 1
fi

input="static/src/app.css"
output="static/css/app.css"

case "${1:-}" in
  --watch)
    exec "$bin" --input "$input" --output "$output" --watch
    ;;
  "")
    exec "$bin" --input "$input" --output "$output" --minify
    ;;
  *)
    echo "usage: $0 [--watch]" >&2
    exit 2
    ;;
esac
