#!/bin/bash
# pilot-45056.sh — C3 protocol + final-candidate config (B=12288: C=256
# per-layer top-n, L2=16, K=8), single tier 45056 x3, with per-miss timing.
# Purpose: before the ~5h final-acceptance, measure whether the candidate
# reaches the 50% gate (>=8.9 tps vs baseline 17.84 tps) at the best-case
# tier (page-cache warm for this fixture) and capture the miss cost split.
set -u
set -o pipefail
cd /home/rm01/models/dev/qwen4-thor
W=.q4t-work/moe-residency-20260930
TAG=pilot-45056-c6
OUT=$W/$TAG
MODEL=~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream
PORT=8151
FIX=$W/e2e-fixtures-v2
HOT=$W/hot-lists/hot-final-12288.json
BIN=build/q4t
rm -rf "$OUT"; mkdir -p "$OUT"
log() { echo "[$(date -Is)] $*" | tee -a "$OUT/pilot.log"; }

if pgrep -f 'q4t serve' > /dev/null; then log "FATAL: q4t serve running"; exit 1; fi
log "=== pilot start; binary sha=$(sha256sum "$BIN" | cut -d' ' -f1 | cut -c1-16) ==="

# [1] drop_caches (C3 protocol step 1)
grep -E '^(Cached|MemAvailable)' /proc/meminfo > "$OUT/meminfo-01-pre-drop.txt"
sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'
sleep 2
grep -E '^(Cached|MemAvailable)' /proc/meminfo > "$OUT/meminfo-02-post-drop.txt"
log "drop_caches done"

# [2] server startup (candidate config, per-miss timing on)
Q4T_RESIDENCY_TIMING=1 Q4T_MOE_L2_SLOTS=16 Q4T_MOE_MIRROR_K=8 Q4T_MOE_MAX_OPEN_SHARDS=200 \
  "$BIN" serve --model-dir "$MODEL" --port $PORT --max-seq 1 \
  --max-prefill 8192 --max-len 262144 --max-tokens 256 --no-mtp \
  --moe-resident-slots 256 --moe-hot-list "$HOT" \
  > "$OUT/server.log" 2>&1 &
SRV=$!
for i in $(seq 1 900); do
  grep -q 'serving on port' "$OUT/server.log" && break
  kill -0 $SRV 2>/dev/null || { log "server died during startup"; tail -20 "$OUT/server.log" | tee -a "$OUT/pilot.log"; exit 1; }
  sleep 1
done
grep -q 'serving on port' "$OUT/server.log" || { log "FATAL: startup timeout"; kill $SRV 2>/dev/null; exit 1; }
grep -E '\[budget\]' "$OUT/server.log" | head -5 | tee -a "$OUT/pilot.log"
grep -E 'Cached|MemAvailable' /proc/meminfo > "$OUT/meminfo-03-post-load.txt"
log "server up (pid $SRV)"

# [3] warmup: 45056 x1, max_tokens=8 (discarded; cold pread evidence)
python3 - "$PORT" "$FIX" 45056 8 "$OUT/warmup.json" <<'PY'
import json, sys, time, urllib.request
port, fix, length, mt, outp = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
prompt = json.loads(open(f'{fix}/context-{length}/requests.jsonl').readline())['prompt']
body = json.dumps({'prompt': prompt, 'max_tokens': mt, 'temperature': 0, 'stream': True}).encode()
req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/chat/completions',
    data=body, headers={'Content-Type': 'application/json'})
t0 = time.time()
n = 0
with urllib.request.urlopen(req, timeout=7200) as r:
    for line in r:
        if line.decode().strip().startswith('data:'):
            n += 1
json.dump({'in': length, 'out_max': mt, 'chunks': n,
           'wall_s': round(time.time()-t0, 1),
           'note': 'warmup, discarded, not counted'}, open(outp, 'w'), indent=2)
print('warmup done', flush=True)
PY
rc=$?
[ $rc -eq 0 ] || { log "FATAL: warmup failed rc=$rc"; kill $SRV 2>/dev/null; exit 1; }
grep -E 'Cached|MemAvailable' /proc/meminfo > "$OUT/meminfo-04-post-warmup.txt"
log "warmup done"

# [4] measurement: 45056 x3, max_tokens=256, SSE timing
python3 - "$PORT" "$FIX" "$OUT" <<'PY'
import json, re, sys, time, urllib.request
port, fix, out = sys.argv[1], sys.argv[2], sys.argv[3]
prompt = json.loads(open(f'{fix}/context-45056/requests.jsonl').readline())['prompt']
def last_residency_line():
    pat = re.compile(r'\[q4t\]\[residency\] id=(\S+) finish=(\S+) in=(\d+) out=(\d+)')
    last = None
    for ln in open(f'{out}/server.log', errors='replace'):
        m = pat.search(ln)
        if m:
            last = m.groups()
    return last
runs = []
for i in range(3):
    body = json.dumps({'prompt': prompt, 'max_tokens': 256, 'temperature': 0,
                       'stream': True}).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/chat/completions',
                                 data=body, headers={'Content-Type': 'application/json'})
    t0 = time.time(); ttft = None; fin = None; n_chunks = 0
    with urllib.request.urlopen(req, timeout=7200) as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith('data:'):
                continue
            payload = line[5:].strip()
            if payload == '[DONE]':
                break
            d = json.loads(payload)
            ch = (d.get('choices') or [{}])[0]
            piece = (ch.get('delta') or {}).get('content')
            if piece:
                n_chunks += 1
                if ttft is None:
                    ttft = time.time() - t0
            if ch.get('finish_reason'):
                fin = ch['finish_reason']
    lat = time.time() - t0
    srv = last_residency_line()
    in_n = int(srv[2]) if srv else None
    out_n = int(srv[3]) if srv else None
    tps = (out_n - 1) / (lat - ttft) if (ttft and out_n and lat > ttft) else None
    runs.append({'ttft': ttft, 'latency': lat, 'out': out_n, 'in': in_n,
                 'chunks': n_chunks, 'finish': fin, 'decode_tps': tps,
                 'server_id': srv[0] if srv else None})
    f = lambda v, s: ('n/a' if v is None else format(v, s))
    print(f'run{i+1}: ttft={f(ttft, ".2f")}s lat={f(lat, ".2f")}s in={in_n} '
          f'out={out_n} chunks={n_chunks} finish={fin} decode={f(tps, ".2f")} tps', flush=True)
json.dump(runs, open(f'{out}/measured.json', 'w'), indent=1)
PY
rc=$?
[ $rc -eq 0 ] || log "WARNING: measurement rc=$rc"

# [5] stop + extract timing evidence
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
grep -E 'Cached|MemAvailable' /proc/meminfo > "$OUT/meminfo-05-post-stop.txt"
log "server stopped"
echo '--- [residency] stats lines ---' | tee -a "$OUT/pilot.log"
grep -h '\[q4t\]\[residency\] id=' "$OUT/server.log" | tee -a "$OUT/pilot.log"
echo '--- [residency][timing] lines ---' | tee -a "$OUT/pilot.log"
grep -h '\[q4t\]\[residency\]\[timing\]' "$OUT/server.log" | tee -a "$OUT/pilot.log"
echo '--- mirror ring stats (if any) ---' | tee -a "$OUT/pilot.log"
grep -hi 'mirror' "$OUT/server.log" | tail -5 | tee -a "$OUT/pilot.log"
log "=== pilot done rc=$? ==="
