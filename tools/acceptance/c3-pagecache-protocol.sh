#!/bin/bash
# c3-pagecache-protocol.sh — C3 page-cache acceptance protocol
# (MOE_RESIDENCY_PATH_C_DESIGN_2026-10-01.md §7).
#
# Purpose: fix the kernel page-cache state before an acceptance matrix and
# record the C3 evidence:
#   (a) global page-cache delta (diagnostic, not model attribution),
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
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel) || exit 1
cd "$ROOT" || exit 1
W=${Q4T_ACCEPTANCE_WORK_DIR:-$ROOT/.q4t-work/moe-residency-20260930}
TAG=${1:?usage: c3-pagecache-protocol.sh <tag> <slots> <hot-list|none> <l2> <binary> [--acceptance]}
SLOTS=${2:?resident-slots (0 = all-experts-resident baseline)}
HOT=${3:?hot-list path or "none"}
L2=${4:?l2-slots (0 disables)}
BIN=${5:?q4t binary path}
MODE=prepare
[ "${6:-}" = "--acceptance" ] && MODE=acceptance
OUT=$W/c3-$TAG
[[ "$TAG" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || {
  echo 'FATAL: invalid evidence tag' >&2; exit 1;
}
MODEL=${Q4T_MODEL_DIR:-$HOME/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream}
PORT=8151
FIX=${Q4T_ACCEPTANCE_FIXTURES:-$ROOT/.q4t-work/moe-residency-20260930/e2e-fixtures-v2}
WARM_LEN=45056
WARM_MAX_TOKENS=8
UNIQUE_EXPERTS=24576
mkdir -p "$W" || exit 1
mkdir "$OUT" || { echo "FATAL: evidence exists: $OUT" >&2; exit 1; }
log() { echo "[$(date -Is)] $*" | tee -a "$OUT/protocol.log"; }
SRV=""
SRV_RC=0
MON=""
MON_RC=0
PROBE_STARTED=0
MON_DIR=$OUT/memory
stop_server() {
  if [[ -n "$SRV" ]]; then
    printf '%s\n' shutdown > "$OUT/memory-phase.txt"
    kill "$SRV" 2>/dev/null || true
    wait "$SRV" 2>/dev/null || SRV_RC=$?
    (set -o noclobber
     printf '{"pid":%d,"server_rc":%d}\n' \
       "$SRV" "$SRV_RC" > "$OUT/server-exit.json") || SRV_RC=1
    SRV=""
  fi
}
stop_monitor() {
  if [[ -n "$MON" ]]; then
    local reason=controller_cleanup
    if [[ "$PROBE_STARTED" -eq 1 ]]; then
      # A sample may have seen the PID just before wait reaped it. Let the
      # next sample observe after_exit before requesting controller stop.
      reason=natural_exit
      for ((i=0; i<150; ++i)); do
        kill -0 "$MON" 2>/dev/null || break
        sleep 0.1
      done
      kill -0 "$MON" 2>/dev/null && reason=natural_exit_timeout
    fi
    if kill -0 "$MON" 2>/dev/null; then
      touch "$MON_DIR/stop" || {
        MON_RC=1
        kill "$MON" 2>/dev/null || true
      }
    fi
    wait "$MON" || MON_RC=$?
    MON=""
    (set -o noclobber
     printf '{"monitor_rc":%d,"controller_stop_reason":"%s"}\n' \
       "$MON_RC" "$reason" > "$OUT/monitor-exit.json") || {
      MON_RC=1;
    }
  fi
}
cleanup() {
  local rc=$?
  trap - EXIT
  stop_server
  stop_monitor
  [[ "$rc" -ne 0 ]] || rc=$SRV_RC
  [[ "$rc" -ne 0 ]] || rc=$MON_RC
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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
sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches' || {
  log 'FATAL: drop_caches failed'; exit 1;
}
sleep 2
meminfo > "$OUT/meminfo-02-post-drop.txt"
log "drop_caches done; meminfo snapshots 01/02 written"

# --- [2] server startup with per-miss timing ------------------------------
# Start before the probe, including its model loading and warmup. The matrix
# runs a different process and writes its own independent memory evidence.
mkdir "$MON_DIR" || exit 1
printf '%s\n' startup > "$OUT/memory-phase.txt"
python3 "$ROOT/tools/evalscope/monitor_memory.py" --model-dir "$MODEL" \
  --pid-file "$OUT/server.pid" --phase-file "$OUT/memory-phase.txt" \
  --ready-file "$MON_DIR/ready" --stop-file "$MON_DIR/stop" \
  --out "$MON_DIR" --interval 1 > "$MON_DIR/monitor.log" 2>&1 &
MON=$!
for ((i=0; i<100; ++i)); do
  [[ -f "$MON_DIR/ready" ]] && break
  kill -0 "$MON" 2>/dev/null || {
    log 'FATAL: probe memory monitor failed before launch'; exit 1;
  }
  sleep 0.1
done
[[ -f "$MON_DIR/ready" ]] && kill -0 "$MON" 2>/dev/null || {
  log 'FATAL: probe memory monitor did not become ready'; exit 1;
}
SERVE_ARGS=(serve --model-dir "$MODEL" --port $PORT --max-seq 1
            --max-prefill 8192 --max-len 262144 --max-tokens 256 --no-mtp)
[ "$SLOTS" != "0" ] && SERVE_ARGS+=(--moe-resident-slots "$SLOTS" --moe-hot-list "$HOT")
MAXOPEN=${Q4T_MOE_MAX_OPEN_SHARDS:-200}
log "Q4T_MOE_MAX_OPEN_SHARDS=$MAXOPEN (C6; 0=all shards in index)"
Q4T_RESIDENCY_TIMING=1 Q4T_MOE_L2_SLOTS=$L2 Q4T_MOE_MAX_OPEN_SHARDS=$MAXOPEN \
  "$BIN" "${SERVE_ARGS[@]}" > "$OUT/server.log" 2>&1 &
SRV=$!
PROBE_STARTED=1
printf '%s\n' "$SRV" > "$OUT/server.pid" || exit 1
python3 - "$OUT" "$BIN" "$HOT" "$SRV" "$L2" "$MAXOPEN" <<'IDENTITY'
import hashlib, json, os, pathlib, sys
out, binary, hot, pid, l2, maxopen = sys.argv[1:]
env = {k: v for k, v in os.environ.items() if k.startswith('Q4T_')}
env.update(Q4T_RESIDENCY_TIMING='1', Q4T_MOE_L2_SLOTS=l2,
           Q4T_MOE_MAX_OPEN_SHARDS=maxopen)
def identity(path):
    p = pathlib.Path(path).resolve()
    return {'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
json.dump({'binary': identity(binary),
           'hot_list': identity(hot) if hot != 'none' else None,
           'pid': int(pid), 'effective_q4t_environment': env},
          open(pathlib.Path(out) / 'protocol-config.json', 'x'), indent=2)
IDENTITY
[ $? -eq 0 ] || { log 'FATAL: identity capture failed'; exit 1; }
for i in $(seq 1 600); do
  kill -0 "$MON" 2>/dev/null || {
    log 'FATAL: probe memory monitor died during startup'; exit 1;
  }
  grep -q 'serving on port' "$OUT/server.log" && break
  kill -0 $SRV 2>/dev/null || { log "server died during startup"; tail -20 "$OUT/server.log" | tee -a "$OUT/protocol.log"; exit 1; }
  sleep 1
done
grep -q 'serving on port' "$OUT/server.log" || { log "FATAL: startup timeout"; exit 1; }
meminfo > "$OUT/meminfo-03-post-load.txt"
log "server up (pid $SRV); meminfo-03-post-load written"

# --- [3] controlled warmup: 45056 x1, max_tokens=8, discarded -------------
printf '%s\n' warmup > "$OUT/memory-phase.txt"
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
if (u.get('prompt_tokens') != length or u.get('completion_tokens') != mt or
        d['choices'][0].get('finish_reason') != 'length'):
    raise RuntimeError('warmup HTTP token/finish contract failed')
print(json.dumps({
    'finish': d['choices'][0].get('finish_reason'),
    'in': u.get('prompt_tokens'),
    'out': u.get('completion_tokens'),
    'wall_s': round(time.time() - t0, 1),
    'note': 'warmup request, discarded, not counted in acceptance stats',
}, indent=2))
PY
rc=$?
[ $rc -eq 0 ] || { log "FATAL: warmup request failed (rc=$rc)"; tail -5 "$OUT/warmup.json" | tee -a "$OUT/protocol.log"; exit 1; }
kill -0 "$MON" 2>/dev/null || {
  log 'FATAL: probe memory monitor died during warmup'; exit 1;
}
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
    'page_cache_delta_gb': round(delta_kb * 1024 / 1e9, 3) if delta_kb is not None else None,
    'pread_avg_ms': round(float(tm.group(1)) / 1000, 3) if tm else None,
    'loads': int(fin.group(1)) if fin else None,
    'reread_factor': round(int(fin.group(1)) / unique, 2) if fin else None,
    'note': 'Global Cached(post-warmup) - Cached(post-load); diagnostic growth only, not model attribution or a physical-memory peak',
}
json.dump(evidence, open(f'{out}/evidence.json', 'w'), indent=2)
print('EVIDENCE:', json.dumps(evidence))
PY
rc=$?
[ $rc -eq 0 ] || { log "FATAL: evidence extraction failed"; exit 1; }

# --- [5] stop probe server; page cache stays warm --------------------------
stop_server
# Reap the target first so the monitor records its final after_exit sample.
stop_monitor
[[ "$SRV_RC" -eq 0 ]] || {
  log "FATAL: probe server failed rc=$SRV_RC"; exit "$SRV_RC";
}
[[ "$MON_RC" -eq 0 ]] || {
  log "FATAL: probe memory monitor failed rc=$MON_RC"; exit "$MON_RC";
}
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
      Q4T_ACCEPTANCE_BINARY="$BIN" Q4T_ACCEPTANCE_WORK_DIR="$W" \
      Q4T_ACCEPTANCE_FIXTURES="$FIX" \
      bash "$ROOT/tools/acceptance/run-e2e.sh" "$TAG"
  else
    Q4T_MOE_L2_SLOTS="$L2" Q4T_RESIDENCY_TIMING=1 \
      Q4T_MOE_MAX_OPEN_SHARDS="$MAXOPEN" \
      Q4T_ACCEPTANCE_BINARY="$BIN" Q4T_ACCEPTANCE_WORK_DIR="$W" \
      Q4T_ACCEPTANCE_FIXTURES="$FIX" \
      bash "$ROOT/tools/acceptance/run-e2e.sh" "$TAG" \
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
ev['warm_pread_avg_us'] = warm
ev['warm_pread_avg_ms'] = round(warm / 1000, 6) if warm is not None else None
ev['warm_timed_requests'] = len(vals)
ev['warm_note'] = ('mean pread_avg_us across all timed acceptance-matrix '
                   'requests (page-cache warm state); cold value is '
                   'pread_avg_ms from the warmup request')
json.dump(ev, open(ev_path, 'w'), indent=2)
print('WARM EVIDENCE:', json.dumps({k: ev[k] for k in
      ('warm_pread_avg_us', 'warm_pread_avg_ms', 'warm_timed_requests')}))
WARM
    [ $? -eq 0 ] || { log 'FATAL: warm timing extraction failed'; exit 1; }
  fi
  # Diagnostic global cache delta only. The matrix server has already exited;
  # this is neither model attribution nor a running physical-memory peak.
  # The independent memory gate consumes the complete sampling evidence.
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
ev['page_cache_delta_matrix_gb'] = round(delta * 1024 / 1e9, 3) if delta is not None else None
ev['matrix_pc_note'] = ('Global Cached(post-matrix, server stopped) - '
                        'Cached(post-load); diagnostic only, not model '
                        'attribution or the memory-budget acceptance number')
json.dump(ev, open(ev_path, 'w'), indent=2)
print('MATRIX PC EVIDENCE:', json.dumps({k: ev[k] for k in
      ('page_cache_delta_matrix_kb', 'page_cache_delta_matrix_gb')}))
MPC
  [ $? -eq 0 ] || { log 'FATAL: cache evidence extraction failed'; exit 1; }
  exit $rc
fi
log "=== c3-$TAG prepare-only done (page cache warm; run acceptance next) ==="
exit 0
