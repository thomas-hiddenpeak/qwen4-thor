# Upstream first-fork investigation

Status: capture validated; root cause pending. See
[the capture result](../../../docs/UPSTREAM_FIRST_FORK_CAPTURE_2026-10-19.md).

`q4t_upstream_first_fork` is an explicit EXCLUDE_FROM_ALL executable. It reuses
the real-text capture lifecycle and adds decoder, GEMV, small-M and cuBLASLt
link wrappers. Actual decoder layer IDs establish scope; actual weight pointers
identify projections. No production source changes or default target hooks.
Captures cover layers 0..2 for the four continuation tokens, including full
state boundaries. The layer-2 tail is a control, not an expanded search scope.

Run only after freezing a bounded plan, with new output directories:

```bash
python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/upstream-example/lease -- \
  python3 -B tools/verify/upstream_first_fork/run.py \
  --binary .q4t-work/moe-replay-a1-20261019/build/q4t_upstream_first_fork \
  --model /home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream \
  --output .q4t-work/upstream-example/run
```

This investigation intentionally requires the previous real-text raw capture at
`.q4t-work/moe-replay-real-20261019/gpu-v1/capture`. Seven natural layer-2
boundaries and the exact token file must reproduce. New binary/source hashes
are retained; model/weight/tokenizer hashes must match. `run.json` PASS is a
capture/control result, not a root-cause or full-model acceptance decision.

`analyze.analyze(capture_path, previous_capture_path)` independently rechecks
425 events, exact shapes, hashes, finiteness, full token-prefix identities,
initial recurrent states and actual observed projection dispatches. It compares
aligned logical rows and supplies exact references for the first 16 changed
outputs per equal-input projection. Raw arrays preserve all other outputs.
State before/after arrays are whole-invocation boundaries; they do not reveal
intermediate T4 state. Do not infer its equality from matching BF16 outputs.

`reference.py` uses integer dyadic products and sums, followed by exact rational
nearest-even BF16 rounding. The result is a mathematical reference, not a demand
that an FP32 reduction always produce correctly rounded BF16. Its host tests
cover cancellation, midpoint ties, subnormals, overflow and invalid input.

```bash
python3 -B tools/verify/upstream_first_fork/test_reference.py -v
python3 -B tools/verify/upstream_first_fork/test_events.py -v
```

No model loading or GPU calls occur in these tests. Payloads and test fixtures
are kept in `.q4t-work/`; the model directory remains read-only.
