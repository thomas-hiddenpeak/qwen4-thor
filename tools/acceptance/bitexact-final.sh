#!/bin/bash
# Bit-exact check: all-resident (slots=0) vs FINAL per-layer top-n
# (hot-final-<B>.json, cap --moe-resident-slots = max C_l), same prompts
# as bitexact-c256.sh. Usage: bitexact-final.sh <B in 7680|8448|9216|9984>
set -u
cd /home/rm01/models/dev/qwen4-thor
B=${1:?usage: bitexact-final.sh <B>}
W=.q4t-work/moe-residency-20260930
MAXC=$(python3 -c "import json; print(max(json.load(open('$W/hot-lists/cap-final-$B.json')).values()))")
export Q4T_MOE_MIRROR_K=${Q4T_MOE_MIRROR_K:-8}  # C4 ring (final candidate); 0 = rollback boundary
OUT=$W/bitexact-final-$B
mkdir -p "$OUT"
MODEL=~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream
PORT=8143
run_server() {  # $1 = tag, rest = extra args
  local tag=$1; shift
  ./build/q4t serve --model-dir "$MODEL" --port $PORT --max-seq 1 \
    --max-prefill 8192 --max-len 262144 --max-tokens 256 --no-mtp "$@" \
    > "$OUT/server-$tag.log" 2>&1 &
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
for tag in base candfinal; do
  if [ "$tag" = base ]; then EXTRA=(); else EXTRA=(--moe-resident-slots "$MAXC" --moe-hot-list "$W/hot-lists/hot-final-$B.json"); fi
  run_server "$tag" "${EXTRA[@]}" || exit 1
  for len in 1024 8192; do send "$tag" "$len" || exit 1; done
  kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
done
echo "=== compare (B=$B, maxC_l=$MAXC) ==="
FAIL=0
for len in 1024 8192; do
  python3 - "$OUT" "$len" <<'PY' || FAIL=1
import json, sys
out, length = sys.argv[1], sys.argv[2]
a = json.load(open(f'{out}/resp-base-{length}.json'))
b = json.load(open(f'{out}/resp-candfinal-{length}.json'))
ta = a['choices'][0]['message']['content']
tb = b['choices'][0]['message']['content']
ua, ub = a.get('usage', {}), b.get('usage', {})
same = (ta == tb) and (ua.get('prompt_tokens') == ub.get('prompt_tokens')) \
       and (ua.get('completion_tokens') == ub.get('completion_tokens'))
print(f'len={length}: BIT-EXACT={same} out={ua.get("completion_tokens")}/{ub.get("completion_tokens")}')
if not same:
    print('  base finish', a['choices'][0].get('finish_reason'), 'cand finish', b['choices'][0].get('finish_reason'))
    for i,(x,y) in enumerate(zip(ta,tb)):
        if x!=y: print(f'  first diff at char {i}: base={ta[i-20:i+20]!r} cand={tb[i-20:i+20]!r}'); break
    sys.exit(1)
PY
done
echo "=== residency stats (candfinal) ==="
grep "\[residency\]" "$OUT/server-candfinal.log" | tail -4 || echo "(no residency lines)"
exit $FAIL
