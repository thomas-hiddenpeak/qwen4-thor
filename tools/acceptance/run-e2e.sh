#!/bin/bash
# Full E2E acceptance for one configuration (baseline or candidate).
# Usage: run-e2e.sh <tag> [extra serve args...]
set -u
cd /home/rm01/models/dev/qwen4-thor
TAG=$1; shift
OUT=.q4t-work/moe-residency-20260930/e2e-$TAG
# Fresh output dir: run_acceptance.py rejects a non-empty output dir, so
# nothing (runner log, monitor output) may be created inside it before the
# runner validates it.
rm -rf "$OUT"
RUNNER_LOG=.q4t-work/moe-residency-20260930/runner-$TAG.log
MODEL=~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream
FIX=.q4t-work/moe-residency-20260930/e2e-fixtures-v2
EXTRA_ARGS=("$@")

python3 tools/evalscope/run_acceptance.py \
  --mode performance \
  --binary build/q4t \
  --model-dir "$MODEL" \
  --port 8000 \
  --max-len 262144 \
  --extra-lengths 261887 \
  --target-total 262144 \
  --fixtures "$FIX" \
  --output "$OUT" \
  --startup-timeout 600 \
  --request-deadline-ms 10800000 \
  --allow-unqualified-binary \
  "${EXTRA_ARGS[@]}" > "$RUNNER_LOG" 2>&1 &
RUNNER=$!

# The runner creates $OUT right after validating it is empty; start the
# memory monitor only once the directory exists so the monitor's own output
# dir does not trip that validation. Model load takes ~30s, so the monitor
# is up well before any memory peak.
MON=""
for i in $(seq 1 120); do [ -d "$OUT" ] && break; sleep 0.5; done
if [ -d "$OUT" ]; then
  tools/evalscope/.venv/bin/python tools/evalscope/monitor_memory.py \
    --name q4t --out "$OUT/memory" --interval 1 &
  MON=$!
fi

wait $RUNNER
RC=$?
# Give the monitor a moment to catch the final sample after the server exits.
sleep 5
if [ -n "$MON" ]; then kill $MON 2>/dev/null; wait $MON 2>/dev/null; fi
cp "$RUNNER_LOG" "$OUT/runner.log" 2>/dev/null || true
{
  echo "runner_rc=$RC"
  echo "tag=$TAG"
  echo "date=$(date -Iseconds)"
} > "$OUT/exit.json" 2>/dev/null || echo "runner_rc=$RC tag=$TAG" > "$TAG.exit.json"
exit $RC
