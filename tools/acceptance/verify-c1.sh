#!/bin/bash
# C1+C2 binary verification (path-c branch df02f60; binary ba7327a9 in
# .q4t-work/wt-c1/build). Run ONLY when no matrix/serve process is running
# (GPU contention would pollute throughput; fault/cancel need a quiet GPU).
# Config: C=256 + hot-256 + L2-16 (C1 default floor; ledger 1.5: pinned
# residency total 4.68 GB).
# Q4T_MOE_MIRROR_K (default 8) selects the C4 eviction-mirror ring size;
# 0 runs the C1+C2-only behavior (rollback boundary). Set it before
# launching this script; it propagates to every server start below.
# 6 checks:
#   [1/6] q4t_tests (wt-c1 build, expect 106/106)
#   [2/6] bitexact-c256 (C=0 vs C=256, tiers 1024/8192)
#   [3/6] cross-binary C=0 identity vs r2-baseline-s0 outputs
#   [4/6] affected-fault (one-shot fault hook + error chain)
#   [5/6] affected-cancel (mid-prefill/mid-decode cancel + resend)
#   [6/6] budget cross-check ([q4t][budget] vs ledger 1.5) + 45056 targeted
#         TTFT/decode, 3 runs vs r3-cand-c256 45056 runs (C1 exit criterion)
# Log: verify-c1.log. Exit 0 only when all checks pass.
set -u
set -o pipefail
cd /home/rm01/models/dev/qwen4-thor
W=.q4t-work/moe-residency-20260930
WT=.q4t-work/wt-c1
BIN=$WT/build/q4t
TESTS=$WT/build/q4t_tests
MODEL=~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream
PORT=8141
OUT=$W/verify-c1
LOG=$W/verify-c1.log
[ -x "$BIN" ] || { echo "FATAL: $BIN missing"; exit 1; }
rm -rf "$OUT"; mkdir -p "$OUT"
FAIL=0
{
  echo "=== verify-c1 start $(date -Is) ==="
  echo "binary: $(sha256sum "$BIN" | cut -d' ' -f1)"
  echo "path-c commit: $(git -C "$WT" rev-parse HEAD)"
  if pgrep -f 'q4t serve' > /dev/null; then
    echo "FATAL: q4t serve still running; refusing to start"
    exit 1
  fi

  echo
  echo "=== [1/6] q4t_tests ==="
  "$TESTS" > "$OUT/q4t_tests.log" 2>&1
  rc=$?
  tail -5 "$OUT/q4t_tests.log"
  echo "q4t_tests rc=$rc"
  [ $rc -eq 0 ] || FAIL=1

  run_server() {  # $1 = tag, rest = extra args
    local tag=$1; shift
    Q4T_MOE_L2_SLOTS=16 Q4T_MOE_MIRROR_K="${Q4T_MOE_MIRROR_K:-8}" "$BIN" serve --model-dir "$MODEL" --port $PORT \
      --max-seq 1 --max-prefill 8192 --max-len 262144 --max-tokens 256 \
      --no-mtp "$@" > "$OUT/server-$tag.log" 2>&1 &
    SRV=$!
    for i in $(seq 1 600); do
      grep -q 'serving on port' "$OUT/server-$tag.log" && return 0
      kill -0 $SRV 2>/dev/null || { echo "server $tag died"; tail -5 "$OUT/server-$tag.log"; return 1; }
      sleep 1
    done
    echo "server $tag startup timeout"; tail -5 "$OUT/server-$tag.log"; return 1
  }
  send() {  # $1 = tag, $2 = length
    local tag=$1 len=$2
    python3 - "$PORT" "$OUT" "$tag" "$len" <<'PY'
import json, sys, urllib.request
port, out, tag, length = sys.argv[1:5]
prompt = json.loads(open(f'{out}/../baseline/inputs/context-{length}.jsonl').readline())['prompt']
body = json.dumps({'prompt': prompt, 'max_tokens': 256, 'temperature': 0}).encode()
req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/chat/completions',
                             data=body, headers={'Content-Type': 'application/json'})
raw = urllib.request.urlopen(req, timeout=3600).read()
open(f'{out}/resp-{tag}-{length}.json', 'wb').write(raw)
d = json.loads(raw)
u = d.get('usage', {})
print(f'{tag} len={length}: finish={d["choices"][0].get("finish_reason")} '
      f'in={u.get("prompt_tokens")} out={u.get("completion_tokens")}')
PY
  }

  echo
  echo "=== [2/6] bitexact-c256 (C=0 vs C=256, C1 binary) ==="
  for tag in base cand256; do
    if [ "$tag" = base ]; then EXTRA=(); else EXTRA=(--moe-resident-slots 256 --moe-hot-list "$W/hot-lists/hot-256.json"); fi
    run_server "$tag" "${EXTRA[@]}" || FAIL=1
    for len in 1024 8192; do send "$tag" "$len" || FAIL=1; done
    kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
  done
  for len in 1024 8192; do
    python3 - "$OUT" "$len" <<'PY'
import json, sys
out, length = sys.argv[1], sys.argv[2]
a = json.load(open(f'{out}/resp-base-{length}.json'))
b = json.load(open(f'{out}/resp-cand256-{length}.json'))
ta = a['choices'][0]['message']['content']
tb = b['choices'][0]['message']['content']
ua, ub = a.get('usage', {}), b.get('usage', {})
same = (ta == tb) and (ua.get('prompt_tokens') == ub.get('prompt_tokens')) \
       and (ua.get('completion_tokens') == ub.get('completion_tokens'))
print(f'len={length}: BIT-EXACT={same} out={ua.get("completion_tokens")}/{ub.get("completion_tokens")}')
if not same:
    for i,(x,y) in enumerate(zip(ta,tb)):
        if x!=y: print(f'  first diff at char {i}'); break
sys.exit(0 if same else 1)
PY
    rc=$?
    [ $rc -eq 0 ] || FAIL=1
  done

  echo
  echo "=== [3/6] cross-binary C=0 identity (C1 base vs r2-baseline-s0) ==="
  python3 - "$W" "$OUT" <<'PY'
import json, sys
W, out = sys.argv[1], sys.argv[2]
ok = True
for length in (1024, 8192):
    a = json.load(open(f'{out}/resp-base-{length}.json'))
    ta = a['choices'][0]['message']['content']
    ref = open(f'{W}/e2e-r2-baseline-s0/context-{length}/output-0.txt').read()
    same = ta == ref
    print(f'len={length}: CROSS-BINARY-C0-IDENTICAL={same} '
          f'(len {len(ta)}/{len(ref)})')
    if not same:
        ok = False
        for i, (x, y) in enumerate(zip(ta, ref)):
            if x != y:
                print(f'  first diff at char {i}: '
                      f'c1={ta[max(0,i-20):i+20]!r} r2={ref[max(0,i-20):i+20]!r}')
                break
sys.exit(0 if ok else 1)
PY
  rc=$?
  echo "cross-binary rc=$rc"
  [ $rc -eq 0 ] || FAIL=1

  echo
  echo "=== [4/6] affected-fault (C1 binary) ==="
  FOUT=$OUT/fault
  rm -rf "$FOUT"; mkdir -p "$FOUT"
  Q4T_RESIDENCY_FAIL_EXPERT=first Q4T_MOE_L2_SLOTS=16 \
    Q4T_MOE_MIRROR_K="${Q4T_MOE_MIRROR_K:-8}" "$BIN" serve \
    --model-dir "$MODEL" --port $PORT --max-seq 1 --max-prefill 8192 \
    --max-len 262144 --max-tokens 256 --no-mtp \
    --moe-resident-slots 256 --moe-hot-list "$W/hot-lists/hot-256.json" \
    > "$FOUT/server.log" 2>&1 &
  SRV=$!
  for i in $(seq 1 300); do
    curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
    sleep 1
  done
  REQFILE="$FOUT/req.json"
  python3 - "$W" > "$REQFILE" <<'PY'
import json, sys
from pathlib import Path
W = Path(sys.argv[1])
rows = [json.loads(l) for l in
        (W/'affected/business-fixture/requests.jsonl').read_text().splitlines()]
r = next(r for r in rows if '44k' in r['id'])
print(json.dumps({'model': 'qwen3.8-flash-next',
                  'messages': [{'role': 'user', 'content': r['prompt']}],
                  'max_tokens': 128, 'temperature': 0, 'stream': False}))
PY
  HTTP1=$(curl -s -o "$FOUT/step1-body.json" -w '%{http_code}' \
    -X POST "http://127.0.0.1:$PORT/v1/chat/completions" \
    -H 'Content-Type: application/json' --data-binary @"$REQFILE" --max-time 3600)
  echo "step1 http=$HTTP1"
  HTTP2=$(curl -s -o "$FOUT/step2-body.json" -w '%{http_code}' \
    -X POST "http://127.0.0.1:$PORT/v1/chat/completions" \
    -H 'Content-Type: application/json' --data-binary @"$REQFILE" --max-time 3600)
  echo "step2 http=$HTTP2"
  kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
  python3 - "$W" "$HTTP1" "$HTTP2" "$FOUT" <<'PY'
import json, sys
from pathlib import Path
W, http1, http2, fout = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
b1 = json.loads((fout/'step1-body.json').read_text())
b2 = json.loads((fout/'step2-body.json').read_text())
base = json.loads((W/'affected/business-baseline/results.json').read_text())
ref = next(r for r in base if '44k' in r['id'])
ok1 = http1 == '500' and 'fault injection' in json.dumps(b1)
ok2 = http2 == '200' and b2['choices'][0]['message']['content'] == ref['text']
print(f'step1 500+message: {"PASS" if ok1 else "FAIL"}')
print(f'step2 200+bitexact: {"PASS" if ok2 else "FAIL"}')
sys.exit(0 if (ok1 and ok2) else 1)
PY
  rc=$?
  echo "fault rc=$rc"
  [ $rc -eq 0 ] || FAIL=1

  echo
  echo "=== [5/6] affected-cancel (C1 binary) ==="
  rm -rf "$OUT/cancel"
  python3 "$W/affected/affected-cancel-c1.py" \
    --out "$OUT/cancel" \
    --hot-list "$W/hot-lists/hot-256.json" \
    --ref-baseline "$W/affected/business-baseline/results.json" \
    > "$OUT/cancel.log" 2>&1
  rc=$?
  tail -12 "$OUT/cancel.log"
  echo "cancel rc=$rc"
  [ $rc -eq 0 ] || FAIL=1

  echo
  echo "=== [6/6] budget cross-check + 45056 targeted (3 runs) ==="
  run_server budget45056 --moe-resident-slots 256 --moe-hot-list "$W/hot-lists/hot-256.json" || FAIL=1
  grep "\[budget\]" "$OUT/server-budget45056.log" | head -4
  python3 - "$PORT" "$OUT" "$W" <<'PY'
import json, re, sys, time, urllib.request
port, out, W = sys.argv[1], sys.argv[2], sys.argv[3]
prompt = json.loads(open(f'{W}/e2e-fixtures-v2/context-45056/requests.jsonl').readline())['prompt']
# Warm-up request (discarded): the r3 matrix 45056 tier ran after three
# smaller prefills (page cache warm); one discarded run levels the page-
# cache state before the timed runs.
for line in urllib.request.urlopen(urllib.request.Request(
        f'http://127.0.0.1:{port}/v1/chat/completions',
        data=json.dumps({'prompt': prompt, 'max_tokens': 8,
                         'temperature': 0, 'stream': True}).encode(),
        headers={'Content-Type': 'application/json'}), timeout=7200):
    pass
print('warm-up done', flush=True)

def last_residency_line():
    # Authoritative server-side token counts for the last finished request
    # (single stream, serialized requests).
    pat = re.compile(r'\[q4t\]\[residency\] id=(\S+) finish=(\S+) '
                     r'in=(\d+) out=(\d+)')
    last = None
    for ln in open(f'{out}/server-budget45056.log', errors='replace'):
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
    runs.append({'ttft': ttft, 'latency': lat, 'out': out_n,
                 'in': in_n, 'chunks': n_chunks, 'finish': fin,
                 'decode_tps': tps, 'server_id': srv[0] if srv else None})
    f = lambda v, s: ('n/a' if v is None else format(v, s))
    print(f'run{i+1}: ttft={f(ttft, ".2f")}s lat={f(lat, ".2f")}s '
          f'in={in_n} out={out_n} chunks={n_chunks} finish={fin} '
          f'decode={f(tps, ".2f")} tps', flush=True)
json.dump(runs, open(f'{out}/targeted-45056.json', 'w'), indent=1)
ref = json.load(open(f'{W}/e2e-r3-cand-c256/results.json'))
r45 = next(r for r in ref if r['length'] == 45056)
print('--- vs r3-cand-c256 45056 (non-C1 binary, same tier/config) ---')
ok = True
for i, (a, b) in enumerate(zip(runs, r45['metrics'])):
    d_tps = (a['decode_tps'] / b['decode_tps'] - 1) * 100 if a['decode_tps'] else None
    d_ttft = (a['ttft'] / b['ttft'] - 1) * 100 if a['ttft'] else None
    f = lambda v, s: ('n/a' if v is None else format(v, s))
    print(f"run{i+1}: c1 decode={f(a['decode_tps'], '.2f')} vs r3 {b['decode_tps']:.2f} "
          f"({f(d_tps, '+.1f')}%) | c1 ttft={f(a['ttft'], '.1f')}s vs r3 {b['ttft']:.1f}s "
          f"({f(d_ttft, '+.1f')}%)")
    if a['decode_tps'] is None or a['decode_tps'] < b['decode_tps']:
        ok = False
print(f'C1 targeted 45056: {"PASS (all 3 runs faster decode than r3 c256)" if ok else "FAIL/NOTE (see above)"}')
sys.exit(0 if ok else 1)
PY
  rc=$?
  kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
  echo "targeted-45056 rc=$rc"
  [ $rc -eq 0 ] || FAIL=1

  echo
  echo "=== verify-c1 done: FAIL=$FAIL $(date -Is) ==="
} > "$LOG" 2>&1
tail -30 "$LOG"
exit $FAIL
