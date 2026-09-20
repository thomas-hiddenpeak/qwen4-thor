#!/usr/bin/env bash
# E2E throughput baseline for q4t serve (OpenAI-compatible API).
#
# Usage:
#   1. Start the server (separate terminal):
#        ./build/q4t serve --port 8000 --max-seq 8 --max-prefill 8192 --max-len 16384
#   2. Run this script:
#        bash tools/evalscope/run_baseline.sh
#
# Requires the uv env:  cd tools/evalscope && uv venv --python 3.12 &&
#   uv pip install 'evalscope[perf]'
#
# Measures aggregate completion tok/s + TTFT + TPOT across concurrency, the
# "ruler" for the data-flow-driven optimization (docs/DATAFLOW_OPTIMIZATION.md
# §4.5). Baseline 2026-09-20: conc1 21.6 tok/s / conc4 31.4 / conc8 57.1.
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate

MODEL_DIR=/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream
URL=http://localhost:8000/v1

# Short-prompt decode throughput sweep (prompt 256, gen 256, conc 1/4/8).
evalscope perf \
  --model qwen3.8-flash-next \
  --url "$URL" \
  --api openai \
  --tokenizer-path "$MODEL_DIR" \
  --dataset random \
  --min-prompt-length 256 --max-prompt-length 256 \
  --max-tokens 256 \
  --parallel 1 4 8 \
  --number 8 16 16 \
  --no-test-connection \
  --stream

# Long-prompt prefill/TTFT probe (prompt 8192, gen 64, conc 1) — the agent
# workload the user actually runs (40K-200K prompts; 8192 is the max_prefill
# ceiling here).
evalscope perf \
  --model qwen3.8-flash-next \
  --url "$URL" \
  --api openai \
  --tokenizer-path "$MODEL_DIR" \
  --dataset random \
  --min-prompt-length 8192 --max-prompt-length 8192 \
  --max-tokens 64 \
  --parallel 1 \
  --number 4 \
  --no-test-connection \
  --stream
