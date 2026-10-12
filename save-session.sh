#!/usr/bin/env bash
set -euo pipefail

# save-session.sh — extract and save Kiro session transcripts for THIS project.
#
# Kiro stores each session on disk under:
#   ~/.kiro/sessions/<workspace-id>/sess_<uuid>/
#       session.json    (metadata: id, title, createdAt, workspacePaths)
#       messages.jsonl  (the transcript, one JSON object per line)
#
# This script reads those, keeps only the sessions whose workspace is THIS
# repository, and writes one self-contained JSON file per session into
# .kiro/sessions/ at the repo root. Each output is named from the session's own
# id and createdAt (never the wall clock), so a batch run never collides.
#
# It does NOT commit — staging/committing is left to you (feature branch + PR).
#
# Usage:
#   ./save-session.sh                       # all sessions for this repo
#   ./save-session.sh <sess_dir> [...]      # specific session director(ies)
#   cat exported-session.json | ./save-session.sh --stdin   # one-off, raw JSON
#
# Options:
#   --store DIR   Kiro session store (default: ~/.kiro/sessions)
#   --stdin       Read a single pre-exported JSON transcript from stdin
#   -h, --help    Show this help

REPO_ROOT="$(git rev-parse --show-toplevel)"
OUT_DIR="${REPO_ROOT}/.kiro/sessions"
STORE="${HOME}/.kiro/sessions"
MODE="all"
declare -a SESSION_DIRS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --store) STORE="$2"; shift 2 ;;
    --stdin) MODE="stdin"; shift ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "Unknown option: $1" >&2; exit 2 ;;
    *) SESSION_DIRS+=("$1"); MODE="dirs"; shift ;;
  esac
done

mkdir -p "$OUT_DIR"

# ---- one-off stdin mode: save a pre-exported transcript verbatim ----
if [ "$MODE" = "stdin" ]; then
  if [ -t 0 ]; then
    echo "Error: --stdin given but no data on stdin." >&2
    exit 1
  fi
  TS="$(date +"%Y%m%d_%H%M%S")"
  OUT="${OUT_DIR}/session_${TS}.json"
  cat > "$OUT"
  if [ ! -s "$OUT" ]; then rm -f "$OUT"; echo "Error: stdin was empty." >&2; exit 1; fi
  echo "Saved ${OUT}" >&2
  echo "$OUT"
  exit 0
fi

# ---- extract one session dir -> one JSON file; prints the path, or nothing
#      if the session does not belong to this repo. ----
extract_one() {
  REPO_ROOT="$REPO_ROOT" OUT_DIR="$OUT_DIR" python3 - "$1" <<'PY'
import json, os, sys, pathlib

sess_dir = pathlib.Path(sys.argv[1])
repo_root = os.environ["REPO_ROOT"]
out_dir = pathlib.Path(os.environ["OUT_DIR"])

meta_path = sess_dir / "session.json"
msgs_path = sess_dir / "messages.jsonl"
if not meta_path.is_file():
    sys.exit(0)  # not a session dir

try:
    meta = json.load(open(meta_path))
except Exception as e:
    print(f"skip {sess_dir.name}: unreadable session.json ({e})", file=sys.stderr)
    sys.exit(0)

# Keep only sessions whose workspace is THIS repo.
paths = meta.get("workspacePaths") or meta.get("rootPaths") or []
if repo_root not in paths:
    sys.exit(0)

messages = []
if msgs_path.is_file():
    with open(msgs_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # tolerate a partial trailing line

sess_id = meta.get("id") or sess_dir.name
created = (meta.get("createdAt") or "")[:19].replace(":", "").replace("-", "").replace("T", "_")
# short id = the uuid part after sess_, first segment
short = sess_id.replace("sess_", "").split("-")[0]
stem = f"session_{created}_{short}" if created else f"session_{short}"
out_path = out_dir / f"{stem}.json"

doc = {
    "id": sess_id,
    "title": meta.get("title"),
    "createdAt": meta.get("createdAt"),
    "workspacePaths": paths,
    "messageCount": len(messages),
    "messages": messages,
}
with open(out_path, "w") as fh:
    json.dump(doc, fh, indent=2)
print(out_path)
PY
}

# ---- collect the session dirs to process ----
if [ "$MODE" = "dirs" ]; then
  TARGETS=("${SESSION_DIRS[@]}")
else
  TARGETS=()
  for ws in "$STORE"/*/; do
    [ -d "$ws" ] || continue
    for sd in "$ws"sess_*/; do
      [ -d "$sd" ] || continue
      TARGETS+=("${sd%/}")
    done
  done
fi

if [ "${#TARGETS[@]}" -eq 0 ]; then
  echo "No sessions found under ${STORE}." >&2
  exit 1
fi

count=0
for sd in "${TARGETS[@]}"; do
  out="$(extract_one "$sd")"
  if [ -n "$out" ]; then
    echo "saved: $out" >&2
    echo "$out"
    count=$((count + 1))
  fi
done

echo "Done: ${count} session(s) for ${REPO_ROOT} written to ${OUT_DIR}" >&2
echo "(not committed — stage and commit them yourself, e.g. on a feature branch)" >&2
