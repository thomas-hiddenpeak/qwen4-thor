#!/bin/bash
# One immutable E2E evidence directory. Existing tags are never overwritten.
# Usage: run-e2e.sh <tag> [extra serve args...]
# Q4T_ACCEPTANCE_BINARY selects the actual measured binary.
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)
cd "$ROOT"
W=${Q4T_ACCEPTANCE_WORK_DIR:-$ROOT/.q4t-work/moe-residency-20260930}
TAG=${1:?usage: run-e2e.sh <tag> [extra serve args...]}; shift
[[ "$TAG" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || {
  echo 'FATAL: invalid evidence tag' >&2; exit 1;
}
OUT=$W/e2e-$TAG
RUNNER_LOG=$W/runner-$TAG.log
MON_DIR=$W/memory-$TAG
BIN=${Q4T_ACCEPTANCE_BINARY:-$ROOT/build/q4t}
MODEL=${Q4T_MODEL_DIR:-$HOME/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream}
FIX=${Q4T_ACCEPTANCE_FIXTURES:-$ROOT/.q4t-work/moe-residency-20260930/e2e-fixtures-v2}
[[ ! -e "$OUT" && ! -e "$RUNNER_LOG" && ! -e "$MON_DIR" ]] || {
  echo "FATAL: evidence already exists for $TAG; choose a new tag" >&2
  exit 1
}
[[ -x "$BIN" ]] || { echo "FATAL: binary unavailable: $BIN" >&2; exit 1; }
mkdir -p "$W"
mkdir "$MON_DIR"
MON=""
cleanup() {
  if [[ -n "$MON" ]]; then
    touch "$MON_DIR/stop"
    wait "$MON" || true
    MON=""
  fi
}
trap cleanup EXIT

# Sample before launch; the runner writes the exact server PID and phase.
python3 tools/evalscope/monitor_memory.py --model-dir "$MODEL" \
  --pid-file "$OUT/server.pid" \
  --phase-file "$OUT/memory-phase.txt" --ready-file "$MON_DIR/ready" \
  --stop-file "$MON_DIR/stop" --out "$MON_DIR" --interval 1 \
  > "$MON_DIR/monitor.log" 2>&1 &
MON=$!
for ((i=0; i<100; ++i)); do
  [[ -f "$MON_DIR/ready" ]] && break
  kill -0 "$MON" 2>/dev/null || {
    echo 'FATAL: memory monitor failed before launch' >&2; exit 1;
  }
  sleep 0.1
done
[[ -f "$MON_DIR/ready" ]] || {
  echo 'FATAL: memory monitor did not become ready' >&2; exit 1;
}
RC=0
python3 tools/evalscope/run_acceptance.py \
  --mode performance --binary "$BIN" --model-dir "$MODEL" \
  --port "${Q4T_ACCEPTANCE_PORT:-8000}" --max-len 262144 \
  --extra-lengths 261887 --target-total 262144 --fixtures "$FIX" \
  --output "$OUT" --startup-timeout 600 \
  --request-deadline-ms 10800000 --allow-unqualified-binary \
  "$@" > "$RUNNER_LOG" 2>&1 || RC=$?
# The runner has reaped the service, but an in-flight memory sample may
# still reflect a live PID. Allow a final after_exit sample before fallback.
MON_STOP_REASON=natural_exit
for ((i=0; i<150; ++i)); do
  kill -0 "$MON" 2>/dev/null || break
  sleep 0.1
done
if kill -0 "$MON" 2>/dev/null; then
  MON_STOP_REASON=natural_exit_timeout
  touch "$MON_DIR/stop"
fi
MON_RC=0
wait "$MON" || MON_RC=$?
MON=""
# Preserve startup failures too; do not replace runner's structured exit.json.
mkdir -p "$OUT"
mv "$MON_DIR" "$OUT/memory"
cp "$RUNNER_LOG" "$OUT/runner.log"
python3 - "$OUT/wrapper-exit.json" "$RC" "$MON_RC" "$TAG" "$MON_STOP_REASON" <<'PY'
import json, sys
from datetime import datetime, timezone
path, runner, monitor, tag, reason = sys.argv[1:]
with open(path, 'x') as stream:
    json.dump({'runner_rc': int(runner), 'monitor_rc': int(monitor),
               'tag': tag, 'monitor_stop_reason': reason,
               'finished_at': datetime.now(timezone.utc).isoformat()},
              stream, indent=2)
PY
[[ "$RC" -eq 0 ]] || exit "$RC"
exit "$MON_RC"
