#!/bin/bash
# Immutable final acceptance session using the canonical scripts in tools/.
# Usage: final-acceptance.sh <B>
# Inputs: Q4T_ACCEPTANCE_ASSETS_DIR (hot/cap lists + prior stage evidence),
# Q4T_ACCEPTANCE_FIXTURES, BASE_BIN, CAND_BIN. Output sessions are created
# under Q4T_ACCEPTANCE_WORK_DIR; this never overwrites earlier experiments.
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)
cd "$ROOT"
ASSETS=${Q4T_ACCEPTANCE_ASSETS_DIR:-$ROOT/.q4t-work/moe-residency-20260930}
WORK=${Q4T_ACCEPTANCE_WORK_DIR:-$ROOT/.q4t-work/moe-residency-20260930}
B=${1:?usage: final-acceptance.sh <7680|8448|9216|9984|10752|12288>}
case "$B" in 7680|8448|9216|9984|10752|12288) ;; *) echo 'FATAL: invalid B' >&2; exit 1;; esac
CAND_BIN=${CAND_BIN:-$ROOT/build/q4t}
BASE_BIN=${BASE_BIN:-$ROOT/build/q4t}
CAP=$ASSETS/hot-lists/cap-final-$B.json
HOT=$ASSETS/hot-lists/hot-final-$B.json
export Q4T_MOE_MIRROR_K=${Q4T_MOE_MIRROR_K:-8}
export Q4T_ACCEPTANCE_FIXTURES=${Q4T_ACCEPTANCE_FIXTURES:-$ASSETS/e2e-fixtures-v2}
# Require completed prior stages. Identity/dependency reuse still needs review;
# a previous log never qualifies a different runtime binary by itself.
for f in compare-report-r3-c256.txt compare-report-r3-nu15552.txt section5-branch.txt; do
  [[ -s "$ASSETS/$f" ]] || { echo "FATAL: missing prerequisite $f" >&2; exit 1; }
done
tail -1 "$ASSETS/verify-c1.log" | grep -q 'FAIL=0' || {
  echo 'FATAL: verify-c1 evidence does not end in FAIL=0' >&2; exit 1;
}
[[ -x "$BASE_BIN" && -x "$CAND_BIN" && -f "$CAP" && -f "$HOT" ]] || {
  echo 'FATAL: missing binary/cap/hot-list' >&2; exit 1;
}
MAXC=$(python3 - "$CAP" "$HOT" "$B" <<'PY'
import json, sys
cap, hot = (json.load(open(p)) for p in sys.argv[1:3])
assert set(cap) == set(hot) == {str(i) for i in range(48)}, 'expected 48 layers'
assert sum(cap.values()) == int(sys.argv[3]), 'slot budget differs'
for layer, count in cap.items():
    assert isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= 512
    ids = hot[layer]
    assert len(ids) == count and len(set(ids)) == count, 'hot-list capacity differs'
    assert all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < 512 for i in ids)
print(max(cap.values()))
PY
)
if pgrep -f 'q4t serve' >/dev/null || pgrep -f 'run_acceptance.py' >/dev/null; then
  echo 'FATAL: q4t serve / run_acceptance.py running; refusing' >&2; exit 1
fi
mkdir -p "$WORK"
W=$(mktemp -d "$WORK/final-$B-$(date +%Y%m%dT%H%M%S)-XXXXXX")
export Q4T_ACCEPTANCE_WORK_DIR=$W
LOG=$W/final-acceptance.log
log() { echo "[$(date -Is)] $*" | tee -a "$LOG"; }
log "session=$W B=$B maxC=$MAXC"
python3 - "$W/session.json" "$BASE_BIN" "$CAND_BIN" "$CAP" "$HOT" "$B" <<'PY'
import hashlib, json, os, pathlib, sys
out, base, cand, cap, hot, budget = sys.argv[1:]
def identity(path):
    p = pathlib.Path(path).resolve()
    return {'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
json.dump({'slot_budget': int(budget), 'baseline': identity(base),
           'candidate': identity(cand), 'capacity': identity(cap), 'hot': identity(hot),
           'effective_q4t_environment': {k: v for k, v in os.environ.items()
                                       if k.startswith('Q4T_')}},
          open(out, 'x'), indent=2)
PY

log '[1/4] baseline C=0, C3 protocol + full matrix'
RC=0
bash "$ROOT/tools/acceptance/c3-pagecache-protocol.sh" \
  acc-base-c0 0 none 0 "$BASE_BIN" --acceptance || RC=$?
if [[ "$RC" -ne 0 ]]; then log "FATAL: baseline failed rc=$RC"; exit "$RC"; fi
log "[2/4] candidate B=$B, C3 protocol + full matrix"
bash "$ROOT/tools/acceptance/c3-pagecache-protocol.sh" \
  "acc-final-$B" "$MAXC" "$HOT" 16 "$CAND_BIN" --acceptance || RC=$?
if [[ "$RC" -ne 0 ]]; then log "FATAL: candidate failed rc=$RC"; exit "$RC"; fi

log '[3/4] compare all HTTP, output and performance contracts'
COMPARE_RC=0
python3 "$ROOT/tools/acceptance/compare_e2e.py" acc-base-c0 "acc-final-$B" \
  --work-dir "$W" --minimum-ratio "${Q4T_ACCEPTANCE_MINIMUM_RATIO:-0.5}" \
  > "$W/compare-report.txt" 2>&1 || COMPARE_RC=$?
tail -25 "$W/compare-report.txt" | tee -a "$LOG"
log "compare rc=$COMPARE_RC"

log '[4/4] independent probe and matrix memory accounting'
MEMORY_ARGS=(--c3-dir "$W/c3-acc-final-$B" --budget-bytes 54000000000)
# Historical authorization applies only to measured overrun, not unknown data.
[[ "$B" != 12288 ]] || MEMORY_ARGS+=(--allow-overrun)
PROBE_MEMORY_RC=0
python3 "$ROOT/tools/evalscope/memory_accounting.py" "${MEMORY_ARGS[@]}" \
  --memory-dir "$W/c3-acc-final-$B/memory" \
  --output "$W/memory-gate-probe.json" >> "$LOG" 2>&1 || PROBE_MEMORY_RC=$?
MATRIX_MEMORY_RC=0
python3 "$ROOT/tools/evalscope/memory_accounting.py" "${MEMORY_ARGS[@]}" \
  --memory-dir "$W/e2e-acc-final-$B/memory" \
  --output "$W/memory-gate-matrix.json" >> "$LOG" 2>&1 || MATRIX_MEMORY_RC=$?
# These processes have separate lifetimes. Require both gates; never add
# their component peaks or manufacture a combined physical-memory total.
MEMORY_RC=0
python3 - "$W" "$B" "$COMPARE_RC" "$PROBE_MEMORY_RC" "$MATRIX_MEMORY_RC" <<'PY' || MEMORY_RC=$?
import json, pathlib, sys
work = pathlib.Path(sys.argv[1])
budget = sys.argv[2]
compare, probe_rc, matrix_rc = map(int, sys.argv[3:])
stages = {}
for stage, prefix, rc in [('probe', 'c3', probe_rc), ('matrix', 'e2e', matrix_rc)]:
    report = work / f'memory-gate-{stage}.json'
    memory_dir = work / f'{prefix}-acc-final-{budget}' / 'memory'
    try:
        gate = json.loads(report.read_text())['gate']
    except (OSError, ValueError, KeyError, TypeError):
        gate = None
    evidence_present = all((memory_dir / name).is_file() and
                           (memory_dir / name).stat().st_size > 0
                           for name in ('memory.csv', 'memory-peak.json'))
    if rc == 0 and (not evidence_present or
                    gate not in ('PASS', 'USER_APPROVED_OVERRUN')):
        rc = 2
    stages[stage] = {'accounting_rc': probe_rc if stage == 'probe' else matrix_rc,
                     'rc': rc, 'gate': gate, 'report': str(report),
                     'evidence_present': evidence_present}
memory = next((stage['rc'] for stage in stages.values() if stage['rc']), 0)
json.dump({'compare_rc': compare, 'memory_rc': memory, 'memory': stages,
           'passed': compare == 0 and memory == 0},
          open(work / 'acceptance-exit.json', 'x'), indent=2)
sys.exit(memory)
PY
log "done compare_rc=$COMPARE_RC memory_rc=$MEMORY_RC"
[[ "$COMPARE_RC" -eq 0 ]] || exit "$COMPARE_RC"
exit "$MEMORY_RC"
