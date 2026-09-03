#!/usr/bin/env bash
# Stages 01-04: manifest, working audio, transcription, editorial brief.
#
# Everything stays on this machine. The only file you need to share afterwards
# is the brief, which is plain text.
#
#   bin/run_to_brief.sh /path/to/recording.mp4 [run-id] [whisper-model]

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

if [ $# -lt 1 ]; then
    sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 64
fi

SOURCE="$1"
RUN_ID="${2:-$(date +%Y-%m-%d)-$(basename "${SOURCE%.*}" | tr -cs '[:alnum:]' '-' | sed 's/-*$//')}"
MODEL="${3:-large-v3}"

echo "run id: $RUN_ID"
echo "source: $SOURCE   (read-only, never modified)"

"$PY" "$HERE/00_doctor.py"      --source "$SOURCE"
"$PY" "$HERE/01_manifest.py"    --run-id "$RUN_ID" --source "$SOURCE"
"$PY" "$HERE/02_audio.py"       --run-id "$RUN_ID"
"$PY" "$HERE/03_transcribe.py"  --run-id "$RUN_ID" --model "$MODEL"
"$PY" "$HERE/04_brief.py"       --run-id "$RUN_ID"

RUNS_ROOT="$(cd "$HERE/.." && pwd)/runs"
BRIEF="$RUNS_ROOT/$RUN_ID/30_decisions/brief.md"

cat <<EOF

================================================================
  Stages 01-04 done. Your recording has not been touched.

  Share this one file:
      $BRIEF

  It is plain text: the editorial rules plus your transcript,
  split into numbered sentences. No audio, no video.

  Read it before you send it. It contains everything you said.

  Then save the JSON you get back as:
      $RUNS_ROOT/$RUN_ID/30_decisions/decisions.json

  and run:
      bin/run_to_review.sh $RUN_ID
================================================================
EOF
