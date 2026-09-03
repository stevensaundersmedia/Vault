#!/usr/bin/env bash
# Stages 05-08: resolve boundaries, render the 720p review, QA, subtitles.
#
# Stops at the review gate. Nothing renders at source resolution and nothing
# is uploaded.
#
#   bin/run_to_review.sh <run-id> [whisper-model]

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

if [ $# -lt 1 ]; then
    sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 64
fi

RUN_ID="$1"
MODEL="${2:-large-v3}"
RUNS_ROOT="$(cd "$HERE/.." && pwd)/runs"
DECISIONS="$RUNS_ROOT/$RUN_ID/30_decisions/decisions.json"

if [ ! -f "$DECISIONS" ]; then
    echo "No decision file at:" >&2
    echo "  $DECISIONS" >&2
    echo "Save the JSON from the editorial pass there first." >&2
    exit 66
fi

"$PY" "$HERE/04_brief.py"   --run-id "$RUN_ID" --validate
"$PY" "$HERE/05_resolve.py" --run-id "$RUN_ID"
"$PY" "$HERE/06_review.py"  --run-id "$RUN_ID"
"$PY" "$HERE/08_subs.py"    --run-id "$RUN_ID"

# QA runs last and is allowed to fail without killing the script: its whole
# job is to tell you what is wrong with the render.
set +e
"$PY" "$HERE/07_qa.py" --run-id "$RUN_ID" --model "$MODEL"
QA_STATUS=$?
set -e

RUN_DIR="$RUNS_ROOT/$RUN_ID"
cat <<EOF

================================================================
  Review ready. Watch it before approving anything.

      video:     $RUN_DIR/40_review/review_720p.mp4
      checksum:  $(cat "$RUN_DIR/40_review/review_720p.mp4.sha256" 2>/dev/null | cut -d' ' -f1)
      QA report: $RUN_DIR/50_qa/qa_report.md
      joins:     $RUN_DIR/50_qa/joins_as_heard.md
      sheets:    $RUN_DIR/50_qa/sheet_overview.jpg, 50_qa/joins/
      subtitles: $RUN_DIR/60_subtitles/review.srt

  Nothing has been rendered at source resolution. Nothing uploaded.
EOF

if [ $QA_STATUS -ne 0 ]; then
    echo "  QA verdict: FAIL — read the report before going further."
fi
echo "================================================================"
