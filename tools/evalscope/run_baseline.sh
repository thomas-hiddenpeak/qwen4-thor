#!/usr/bin/env bash
# Single-stream HTTP E2E matrix. See docs/EVALUATION.md.
# Start a freshly built server first (MTP is now off by default):
# ./build/q4t serve --port 8000 --max-seq 1 --max-prefill 8192 \
#   --max-len 208896 --max-tokens 256
# Save that command, server environment and server.log in the result directory.
# Run: Q4T_E2E_OUT=.q4t-work/e2e/<run-name> bash tools/evalscope/run_baseline.sh
# Requires the existing tools/evalscope/.venv with evalscope[perf].
set -euo pipefail
Q4T_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$Q4T_ROOT"
Q4T_EVAL="$Q4T_ROOT/tools/evalscope/.venv/bin/evalscope"
Q4T_PYTHON="$Q4T_ROOT/tools/evalscope/.venv/bin/python"
Q4T_MODEL_DIR=${Q4T_MODEL_DIR:-/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream}
Q4T_URL=${Q4T_URL:-http://127.0.0.1:8000/v1/chat/completions}
Q4T_E2E_OUT=${Q4T_E2E_OUT:-.q4t-work/e2e/$(date +%Y%m%d-%H%M%S)}
Q4T_E2E_OUT=$(realpath -m "$Q4T_E2E_OUT")
case "$Q4T_E2E_OUT" in
  "$Q4T_ROOT"/build/*|"$Q4T_ROOT"/.q4t-work/*) ;;
  *) echo 'Results must be inside build/ or .q4t-work/.' >&2; exit 2 ;;
esac
mkdir -p "$Q4T_E2E_OUT"
git rev-parse HEAD > "$Q4T_E2E_OUT/commit.txt"
git diff > "$Q4T_E2E_OUT/worktree.patch"
git status --short > "$Q4T_E2E_OUT/worktree-status.txt"
sha256sum build/q4t > "$Q4T_E2E_OUT/binary.sha256"
cp "$0" "$Q4T_E2E_OUT/run_baseline.sh"
cp tools/evalscope/prepare_inputs.py "$Q4T_E2E_OUT/prepare_inputs.py"
cp build/CMakeCache.txt "$Q4T_E2E_OUT/CMakeCache.txt"
"$Q4T_PYTHON" -c 'from importlib.metadata import version; print(version("evalscope"))' \
  > "$Q4T_E2E_OUT/evalscope-version.txt"
# These overrides permit resuming an interrupted matrix. A partial run is not
# a complete baseline. Do not compare different sample counts as equal evidence.
read -r -a Q4T_CONTEXTS <<< "${Q4T_CONTEXTS:-1024 4096 8192 45056 204800}"
Q4T_REQUESTS=${Q4T_REQUESTS:-3}
for Q4T_LENGTH in "${Q4T_CONTEXTS[@]}"; do
  Q4T_CASE="$Q4T_E2E_OUT/context-$Q4T_LENGTH"
  if [[ -e "$Q4T_CASE" ]]; then
    echo "Refusing to overwrite $Q4T_CASE" >&2
    exit 2
  fi
  mkdir -p "$Q4T_CASE"
  "$Q4T_PYTHON" tools/evalscope/prepare_inputs.py \
    --model-dir "$Q4T_MODEL_DIR" --length "$Q4T_LENGTH" \
    --number "$Q4T_REQUESTS" --output "$Q4T_CASE/requests.jsonl"
  Q4T_ARGS=(perf --model qwen3.8-flash-next --url "$Q4T_URL" --api openai
    --tokenizer-path "$Q4T_MODEL_DIR" --dataset line_by_line
    --dataset-path "$Q4T_CASE/requests.jsonl"
    --min-prompt-length "$Q4T_LENGTH" --max-prompt-length "$Q4T_LENGTH"
    --no-apply-chat-template --max-tokens 256 --temperature 0 --seed 20260920
    --parallel 1 --number "$Q4T_REQUESTS" --warmup-num 0
    --connect-timeout 30 --read-timeout 7200 --total-timeout 10800
    --no-test-connection --stream --outputs-dir "$Q4T_CASE")
  printf '%q ' "$Q4T_EVAL" "${Q4T_ARGS[@]}" > "$Q4T_CASE/command.txt"
  printf '\n' >> "$Q4T_CASE/command.txt"
  "$Q4T_EVAL" "${Q4T_ARGS[@]}" 2>&1 | tee "$Q4T_CASE/client.log"
  # Inspect the actual E2E report: evalscope can return zero with failed HTTP
  # requests. This is part of E2E acceptance, not a separate preliminary test.
  "$Q4T_PYTHON" - "$Q4T_CASE" "$Q4T_REQUESTS" "$Q4T_LENGTH" <<'PY'
import base64
import json
import pathlib
import pickle
import sqlite3
import sys
from evalscope.perf.utils.perf_models import BenchmarkSummary
reports = list(pathlib.Path(sys.argv[1]).rglob('benchmark_summary.json'))
if len(reports) != 1:
    raise SystemExit(f'Expected one E2E report, got {len(reports)}')
s = BenchmarkSummary.model_validate(json.loads(reports[0].read_text()))
if s.failed_requests or s.succeed_requests != int(sys.argv[2]):
    raise SystemExit('E2E failed; inspect the saved requests and server log')
databases = list(pathlib.Path(sys.argv[1]).rglob('benchmark_data.db'))
if len(databases) != 1:
    raise SystemExit('Missing per-request E2E evidence')
with sqlite3.connect(f'file:{databases[0]}?mode=ro', uri=True) as db:
    rows = db.execute(
        'SELECT success, prompt_tokens, completion_tokens, response_messages '
        'FROM result').fetchall()
for success, input_tokens, output_tokens, encoded in rows:
    # Only read the trusted database produced by this local evalscope run.
    responses = pickle.loads(base64.b64decode(encoded))
    # evalscope 1.12 stores usage in token columns, omitting choices=[]
    # usage chunks from response_messages (default_api.py).
    if not success or input_tokens != int(sys.argv[3]):
        raise SystemExit('E2E input length/usage mismatch; inspect raw responses')
    if output_tokens != 256:
        raise SystemExit('E2E output length differs from the 256-token target')
    if not any(choice.get('finish_reason') == 'length'
               for response in responses for choice in response.get('choices', [])):
        raise SystemExit('E2E response did not finish at the requested output limit')
print('HTTP E2E and server token counts passed; output fidelity still needs review.')
PY
done
