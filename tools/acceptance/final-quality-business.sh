#!/bin/bash
# final-quality-business.sh — fixed quality set + business output
# comparison for the FINAL candidate (C1+C2 binary, per-layer top-n
# hot-final-<B>.json, L2-16) vs C=0 on the SAME binary.
#
# Closes B-selection rule 3 (correctness): fixed 11-question quality
# set (manifest-exact + base-vs-candidate identical) and business
# final_validation subset (6 requests, bit-identical text + token
# counts). Fault/cancel/slot-reuse/cross-request checks are covered by
# verify-c1 [4/6][5/6] + matrix bit-exact 3-request sequences.
#
# Usage: final-quality-business.sh <B in 7680|8448|9216|9984|10752|12288>
# Prereq: no q4t serve / run_acceptance.py running; build/q4t is the
# C1+C2 merged binary (record sha at start).
# Log: final-quality-business.log
set -u
set -o pipefail
cd /home/rm01/models/dev/qwen4-thor
B=${1:?usage: final-quality-business.sh <B in 7680|8448|9216|9984|10752>}
case "$B" in 7680|8448|9216|9984|10752|12288) ;; *) echo "FATAL: bad B=$B"; exit 1;; esac
W=.q4t-work/moe-residency-20260930
LOG=$W/final-quality-business-$B.log
export Q4T_MOE_MIRROR_K=${Q4T_MOE_MIRROR_K:-8}  # C4 ring (final candidate); 0 = rollback boundary
echo_log() { echo "[$(date -Is)] $*" | tee -a "$LOG"; }

echo_log "=== final-quality-business B=$B start ==="
echo_log "binary: $(sha256sum build/q4t | cut -d' ' -f1 | cut -c1-16)"
echo_log "commit: $(git rev-parse HEAD)"
if pgrep -f 'q4t serve' > /dev/null || pgrep -f 'run_acceptance.py' > /dev/null; then
  echo_log "FATAL: q4t serve / run_acceptance.py running; refusing"
  exit 1
fi
CAP=$W/hot-lists/cap-final-$B.json
HOT=$W/hot-lists/hot-final-$B.json
[ -f "$CAP" ] && [ -f "$HOT" ] || { echo_log "FATAL: cap/hot-final-$B missing"; exit 1; }
MAXC=$(python3 -c "import json; print(max(json.load(open('$CAP')).values()))")
echo_log "max C_l=$MAXC total slots=$B"

FIXQ=.q4t-work/moe-trace-runtime-20260928/quality-off/inputs
FIXB=$W/affected/business-fixture
MODEL=~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream
PORT=8003

run_quality() {  # $1 tag, rest extra serve args
  local tag=$1; shift
  python3 tools/evalscope/run_acceptance.py \
    --mode quality --binary build/q4t --model-dir "$MODEL" \
    --port $PORT --max-len 262144 --fixtures "$FIXQ" \
    --output "$W/affected/final-quality-$B-$tag" \
    --startup-timeout 600 --allow-unqualified-binary "$@"
}
run_business() {  # $1 tag, rest extra serve args
  local tag=$1; shift
  python3 "$W/affected/affected_http.py" \
    --requests "$FIXB/requests.jsonl" --out "$W/affected/final-business-$B-$tag" \
    --port $PORT "$@"
}

echo_log "=== [1/4] quality baseline (C=0) ==="
run_quality base || { echo_log "FATAL: quality base failed"; exit 1; }
echo_log "quality-base rc=0"
echo_log "=== [2/4] quality candidate (B=$B, L2-16) ==="
Q4T_MOE_L2_SLOTS=16 run_quality cand \
  --moe-resident-slots "$MAXC" --moe-hot-list "$HOT" \
  || { echo_log "FATAL: quality cand failed"; exit 1; }
echo_log "quality-cand rc=0"
echo_log "=== [3/4] business baseline (C=0) ==="
run_business base || { echo_log "FATAL: business base failed"; exit 1; }
echo_log "business-base rc=0"
echo_log "=== [4/4] business candidate (B=$B, L2-16) ==="
Q4T_MOE_L2_SLOTS=16 run_business cand \
  --moe-resident-slots "$MAXC" --moe-hot-list "$HOT" \
  || { echo_log "FATAL: business cand failed"; exit 1; }
echo_log "business-cand rc=0"

python3 - "$W" "$B" >> "$LOG" 2>&1 <<'PY'
import json, sys
from pathlib import Path
W, B = Path(sys.argv[1]), sys.argv[2]
def load(p):
    return {r['id']: r for r in json.loads((W/p).read_text())}
qb = load(f'affected/final-quality-{B}-base/results.json')
qc = load(f'affected/final-quality-{B}-cand/results.json')
exact = all(r['exact_match'] for r in qc.values())
same = all(qb[i]['text'] == qc[i]['text'] for i in qb)
print(f'quality B={B}: manifest-exact={exact} base-vs-cand identical={same}')
if not (exact and same):
    bad = [i for i in qb if qb[i]['text'] != qc[i]['text']]
    print('  differing ids:', bad); sys.exit(1)
rb = load(f'affected/final-business-{B}-base/results.json')
rc = load(f'affected/final-business-{B}-cand/results.json')
assert set(rb) == set(rc), 'business id sets differ'
ok = True
for i in rb:
    same = (rb[i]['text'] == rc[i]['text'] and
            rb[i]['actual_input'] == rc[i]['actual_input'] and
            rb[i]['actual_output'] == rc[i]['actual_output'])
    print(f'business B={B} {i}: in={rc[i]["actual_input"]} '
          f'out={rc[i]["actual_output"]} identical={"yes" if same else "NO"}')
    if not same:
        ok = False
if not ok:
    sys.exit(1)
print(f'PASS: final-quality-business B={B} (quality manifest-exact + '
      f'identical, business bit-identical x6)')
PY
rc=$?
echo_log "verify rc=$rc"
echo_log "=== final-quality-business B=$B end $(date -Is) ==="
exit $rc
