#!/usr/bin/env bash
set -euo pipefail

# save-session.sh — extract and save a Kiro session transcript.
#
# Reads a session transcript (JSON) on stdin and writes it to a timestamped
# file under .kiro/sessions/ at the repo root. It does NOT commit: committing
# is left to you (e.g. via a feature branch + PR), so you can run this once per
# session and stage/commit the results however you like.
#
# Usage:
#   <produce session JSON> | ./save-session.sh
#   ./save-session.sh < session.json
#
# Example:
#   pbpaste | ./save-session.sh        # from the clipboard (macOS)
#   cat exported-session.json | ./save-session.sh

# Resolve the sessions directory inside the repo.
SESSIONS_DIR="$(git rev-parse --show-toplevel)/.kiro/sessions"
mkdir -p "$SESSIONS_DIR"

TIMESTAMP="$(date +"%Y%m%d_%H%M%S")"
FILE_PATH="${SESSIONS_DIR}/session_${TIMESTAMP}.json"

# Refuse to write an empty file (e.g. if nothing was piped in).
if [ -t 0 ]; then
  echo "Error: no session data on stdin. Pipe the session JSON in, e.g.:" >&2
  echo "  cat session.json | $0" >&2
  exit 1
fi

# Write stdin (the session transcript) to the file.
cat > "$FILE_PATH"

if [ ! -s "$FILE_PATH" ]; then
  rm -f "$FILE_PATH"
  echo "Error: stdin was empty; nothing saved." >&2
  exit 1
fi

echo "Session transcript saved to ${FILE_PATH}" >&2
echo "(not committed — stage and commit it yourself, e.g. on a feature branch)" >&2

# Print the path on stdout so callers can capture it.
echo "${FILE_PATH}"
