#!/bin/bash
# c3-pagecache-protocol.sh — C3 page-cache acceptance protocol
# (MOE_RESIDENCY_PATH_C_DESIGN_2026-10-01.md §7).
#
# Purpose: fix the kernel page-cache state before an acceptance matrix and
# record the C3 evidence:
#   (a) model-related page-cache delta (Cached after warmup - after load),
#   (b) pread_avg change (cold 2.40 ms -> page-cache hit ~0.2-0.3 ms),
#   (c) re-read factor (loads / 24576 unique experts).
# Baseline and candidate MUST use the identical protocol (same warmup,
# same drop_caches cadence) or the comparison is unfair.
#
# Usage:
#   c3-pagecache-protocol.sh <tag> <resident-slots|0> <hot-list|none> \
#                            <l2-slots> <binary> [--acceptance]
#   tag             output tag, e.g. c3-final-c208 / c3-baseline-c0
#   resident-slots  C for this config; 0 = all-experts-resident baseline
#   hot-list        hot-list JSON path, or "none" for C=0
#   l2-slots        L2 slot count (0 disables L2)
#   binary          q4t binary path
#   --acceptance    after warmup+evidence, immediately run the full
#                   acceptance matrix via run-e2e.sh with the same config
#                   (page cache stays warm; NO drop_caches in between).
#
# C6 (2026-10-02): the candidate serve also sets
# Q4T_MOE_MAX_OPEN_SHARDS (default 200 >= the model's 197 shards, so all
# shards stay open). With the old 32-shard LRU, each decode expert miss
# does ~15 EnsureOpen calls on its shard and the shard is evicted
# (munmap) + reopened (open+mmap+200 KB header parse, ~1.5 ms) between
# them, dominating the decode miss critical path (pilot-45056-c5b:
# dpread 5.4 ms/miss, decode 6.45 tps = 36%). Keeping all shards open
# (data still read via pread, no process page faults) cut the decode miss
# to ~2.1 ms (pilot-45056-c6: 10.05 tps = 56% of baseline, over the 50%
# gate). Baseline C=0 is unaffected (loader released after load).
# Override with Q4T_MOE_MAX_OPEN_SHARDS in the environment.
#
# Hard guards:
#   - Refuses to run while any 'q4t serve' or 'run_acceptance.py' process
#     is alive (would pollute the in-flight run's page-cache state).
#   - drop_caches runs exactly once, before server startup.
#   - After warmup, no manual drop_caches or large file reads until the
#     acceptance measurement requests finish.
set -u
set -o pipefail
cd /home/rm01/models/dev/qwen4-thor
W=.q4t-work/moe-residency-20260930
TAG=${1:?usage: c3-pagecache-protocol.sh <tag> <slots> <hot-list|none> <l2> <binary> [--acceptance]}
SLOTS=${2:?resident-slots (0 = all-experts-resident baseline)}
HOT=${3:?hot-list path or "none"}
L2=${4:?l2-slots (0 disables)}
BIN=${5:?q4t binary path}
MODE=prepare
[ "${6:-}" = "--acceptance" ] && MODE=acceptance
OUT=$W/c3-$TAG
MODEL=~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream
PORT=8151
FIX=$W/e2e-fixtures-v2
WARM_LEN=45056
WARM_MAX_TOKENS=8
UNIQUE_EXPERTS=24576
mkdir -p "$OUT"
log() { echo "[$(date -Is)] $*" | tee -a "$OUT/protocol.log"; }

log "=== c3-$TAG start (mode=$MODE, slots=$SLOTS, l2=$L2, bin=$BIN) ==="

# --- guard: no other q4t serve / acceptance in flight ---------------------
if pgrep -f 'q4t serve' > /dev/null; then
  log "FATAL: 'q4t serve' running; refusing (would pollute page-cache state)"
  exit 1
fi
if pgrep -f 'run_acceptance.py' > /dev/null; then
  log "FATAL: 'run_acceptance.py' running; refusing"
  exit 1
fi
[ -x "$BIN" ] || { log "FATAL: binary $BIN missing/not executable"; exit 1; }
[ "$SLOTS" = "0" ] || { [ -f "$HOT" ] || { log "FATAL: hot list $HOT missing"; exit 1; }; }
[ -f "$FIX/context-$WARM_LEN/requests.jsonl" ] || { log "FATAL: warmup fixture missing"; exit 1; }

meminfo() { grep -E '^(Cached|MemAvailable|SwapTotal|SwapFree)' /proc/meminfo; }

# --- [1] cold baseline: drop_caches before server startup -----------------
meminfo > "$OUT/meminfo-01-pre-drop.txt"
sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'
sleep 2
meminfo > "$OUT/meminfo-02-post-drop.txt"
log "drop_caches done; meminfo snapshots 01/02 written"

# --- [2] server startup with per-miss timing ------------------------------
SERVE_ARGS=(serve --model-dir "$MODEL" --port $PORT --max-seq 1
            --max-prefill 8192 --max-len 262144 --max-tokens 256 --no-mtp)
[ "$SLOTS" != "0" ] && SERVE_ARGS+=(--moe-resident-slots "$SLOTS" --moe-hot-list "$HOT")
MAXOPEN=${Q4T_MOE_MAX_OPEN_SHARDS:-200}
log "Q4T_MOE_MAX_OPEN_SHARDS=$MAXOPEN (C6; 0=all shards in index)"
Q4T_RESIDENCY_TIMING=1 Q4T_MOE_L2_SLOTS=$L2 Q4T_MOE_MAX_OPEN_SHARDS=$MAXOPEN \
  "$BIN" "${SERVE_ARGS[@]}" > "$OUT/server.log" 2>&1 &
SRV=$!
for i in $(seq 1 600); do
  grep -q 'serving on port' "$OUT/server.log" && break
  kill -0 $SRV 2>/dev/null || { log "server died during startup"; tail -20 "$OUT/server.log" | tee -a "$OUT/protocol.log"; exit 1; }
  sleep 1
done
grep -q 'serving on port' "$OUT/server.log" || { log "FATAL: startup timeout"; kill $SRV 2>/dev/null; exit 1; }
meminfo > "$OUT/meminfo-03-post-load.txt"
log "server up (pid $SRV); meminfo-03-post-load written"

# --- [3] controlled warmup: 45056 x1, max_tokens=8, discarded -------------
python3 - "$PORT" "$FIX" "$WARM_LEN" "$WARM_MAX_TOKENS" > "$OUT/warmup.json" 2>&1 <<'PY'
import json, sys, time, urllib.request
port, fix, length, mt = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
prompt = json.loads(open(f'{fix}/context-{length}/requests.jsonl').readline())['prompt']
body = json.dumps({'prompt': prompt, 'max_tokens': mt, 'temperature': 0}).encode()
req = urllib.request.Request(
    f'http://127.0.0.1:{port}/v1/chat/completions',
    data=body, headers={'Content-Type': 'application/json'})
t0 = time.time()
raw = urllib.request.urlopen(req, timeout=3600).read()
d = json.loads(raw)
u = d.get('usage', {})
print(json.dumps({
    'finish': d['choices'][0].get('finish_reason'),
    'in': u.get('prompt_tokens'),
    'out': u.get('completion_tokens'),
    'wall_s': round(time.time() - t0, 1),
    'note': 'warmup request, discarded, not counted in acceptance stats',
}, indent=2))
PY
rc=$?
[ $rc -eq 0 ] || { log "FATAL: warmup request failed (rc=$rc)"; tail -5 "$OUT/warmup.json" | tee -a "$OUT/protocol.log"; kill $SRV 2>/dev/null; exit 1; }
cat "$OUT/warmup.json" >> "$OUT/protocol.log"
meminfo > "$OUT/meminfo-04-post-warmup.txt"
log "warmup done; meminfo-04-post-warmup written"

# --- [4] evidence ----------------------------------------------------------
python3 - "$OUT" "$UNIQUE_EXPERTS" >> "$OUT/protocol.log" <<'PY'
import json, re, sys
out, unique = sys.argv[1], int(sys.argv[2])
def cached_kb(path):
    for line in open(path):
        if line.startswith('Cached:'):
            return int(line.split()[1])
    return None
c_load = cached_kb(f'{out}/meminfo-03-post-load.txt')
c_warm = cached_kb(f'{out}/meminfo-04-post-warmup.txt')
delta_kb = (c_warm - c_load) if (c_load is not None and c_warm is not None) else None
srv = open(f'{out}/server.log').read()
tm = re.search(r'\[q4t\][^\n]*pread_avg_us=([\d.]+)[^\n]*', srv)
fin = re.search(r'\[q4t\][^\n]*loads=(\d+)[^\n]*', srv)
evidence = {
    'tag': out.split('/')[-1],
    'page_cache_delta_kb': delta_kb,
    'page_cache_delta_gb': round(delta_kb / 1048576, 3) if delta_kb is not None else None,
    'pread_avg_ms': round(float(tm.group(1)) / 1000, 3) if tm else None,
    'loads': int(fin.group(1)) if fin else None,
    'reread_factor': round(int(fin.group(1)) / unique, 2) if fin else None,
    'note': 'delta = Cached(post-warmup) - Cached(post-load); counts ALL page-cache growth during warmup (model-related here; no other IO expected)',
}
json.dump(evidence, open(f'{out}/evidence.json', 'w'), indent=2)
print('EVIDENCE:', json.dumps(evidence))
PY
rc=$?
[ $rc -eq 0 ] || { log "FATAL: evidence extraction failed"; kill $SRV 2>/dev/null; exit 1; }

# --- [5] stop probe server; page cache stays warm --------------------------
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
meminfo > "$OUT/meminfo-05-post-stop.txt"
log "probe server stopped; page cache remains warm"

if [ "$MODE" = "acceptance" ]; then
  # Immediately run the full acceptance matrix with the same config.
  # NO drop_caches between warmup and these measurement requests.
  log "=== acceptance matrix start (run-e2e.sh, same config) ==="
  # Propagate L2 to the acceptance matrix: run-e2e.sh inherits env, and the
  # server it launches reads Q4T_MOE_L2_SLOTS (default 128 = 20.13 GB when
  # unset). Without this the candidate would allocate L2=128 instead of the
  # protocol's L2, corrupting the 54 GB memory gate. (C=0 baseline never
  # allocates an L2 pool, so the SLOTS=0 branch is a no-op for L2.)
  # Enable per-miss timing on the acceptance matrix so the "warm" pread_avg
  # (page-cache hit ~0.2-0.3 ms) is captured in the matrix server.log,
  # completing evidence (b) cold->warm (design S7.3). Fair: applied to both
  # baseline (C=0, zero miss-path overhead) and candidate. run_acceptance.py
  # does not strip Q4T_RESIDENCY_TIMING, so it is inherited by the server.
  # C6: propagate the max-open-shards cap to the acceptance matrix server
  # too (identical config to the probe server; no-op for the C=0 baseline).
  if [ "$SLOTS" = "0" ]; then
    Q4T_MOE_L2_SLOTS="$L2" Q4T_RESIDENCY_TIMING=1 \
      Q4T_MOE_MAX_OPEN_SHARDS="$MAXOPEN" \
      bash "$W/run-e2e.sh" "$TAG"
  else
    Q4T_MOE_L2_SLOTS="$L2" Q4T_RESIDENCY_TIMING=1 \
      Q4T_MOE_MAX_OPEN_SHARDS="$MAXOPEN" \
      bash "$W/run-e2e.sh" "$TAG" \
      --moe-resident-slots "$SLOTS" --moe-hot-list "$HOT"
  fi
  rc=$?
  log "=== acceptance matrix rc=$rc done ==="
  # Capture the "warm" pread_avg (page-cache hit) from the acceptance
  # matrix server.log, completing evidence (b) cold->warm. The matrix ran
  # with Q4T_RESIDENCY_TIMING=1 (set above), so its server.log has
  # per-request [residency][timing] lines. Average pread_avg_us across all
  # timed requests = the steady-state (warm) miss unit price.
  if [ -f "$W/e2e-$TAG/server.log" ]; then
    python3 - "$OUT" "$W/e2e-$TAG/server.log" >> "$OUT/protocol.log" <<'WARM'
import json, os, re, sys
out, mlog = sys.argv[1], sys.argv[2]
vals = [float(m) for m in re.findall(
    r'\[q4t\]\[residency\]\[timing\][^\n]*pread_avg_us=([\d.]+)',
    open(mlog, errors='replace').read())]
warm = round(sum(vals) / len(vals), 3) if vals else None
ev_path = out + '/evidence.json'
ev = json.load(open(ev_path)) if os.path.isfile(ev_path) else {}
ev['warm_pread_avg_ms'] = warm
ev['warm_timed_requests'] = len(vals)
ev['warm_note'] = ('mean pread_avg_us across all timed acceptance-matrix '
                   'requests (page-cache warm state); cold value is '
                   'pread_avg_ms from the warmup request')
json.dump(ev, open(ev_path, 'w'), indent=2)
print('WARM EVIDENCE:', json.dumps({k: ev[k] for k in
      ('warm_pread_avg_ms', 'warm_timed_requests')}))
WARM
  fi
  # End-of-matrix page-cache snapshot: the acceptance matrix (18 requests,
  # incl. the 204800/261887 tiers larger than the 45056 warmup) may load
  # additional experts, growing the model-related page cache beyond the
  # warmup delta. The 54 GB memory gate (goal req #2) must use the actual
  # page cache at the end of the matrix, not just the warmup delta.
  # delta_matrix = Cached(post-matrix) - Cached(post-load, meminfo-03).
  meminfo > "$OUT/meminfo-06-post-matrix.txt"
  python3 - "$OUT" >> "$OUT/protocol.log" <<'MPC'
import json, os, sys
out = sys.argv[1]
def cached_kb(path):
    for line in open(path):
        if line.startswith('Cached:'):
            return int(line.split()[1])
    return None
c_load = cached_kb(f'{out}/meminfo-03-post-load.txt')
c_mat = cached_kb(f'{out}/meminfo-06-post-matrix.txt')
delta = (c_mat - c_load) if (c_load is not None and c_mat is not None) else None
ev_path = out + '/evidence.json'
ev = json.load(open(ev_path)) if os.path.isfile(ev_path) else {}
ev['page_cache_delta_matrix_kb'] = delta
ev['page_cache_delta_matrix_gb'] = round(delta / 1048576, 3) if delta is not None else None
ev['matrix_pc_note'] = ('Cached(post-matrix) - Cached(post-load); the actual '
                        'model-related page cache at the end of the acceptance '
                        'matrix (supersedes the warmup delta for the 54 GB '
                        'memory gate)')
json.dump(ev, open(ev_path, 'w'), indent=2)
print('MATRIX PC EVIDENCE:', json.dumps({k: ev[k] for k in
      ('page_cache_delta_matrix_kb', 'page_cache_delta_matrix_gb')}))
MPC
  exit $rc
fi
log "=== c3-$TAG prepare-only done (page cache warm; run acceptance next) ==="
exit 0
