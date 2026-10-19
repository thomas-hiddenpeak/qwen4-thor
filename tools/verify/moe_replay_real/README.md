# Real-text MoE diagnostics (implementation in progress)

This directory contains the host contracts for the real-input extension.
The actual model capture/link wrapper and real-input replay driver are still
pending. Passing these tests is not GPU or full-model validation.

## Boundary record version 1

`record.json` has exactly these keys:

- `version`: integer 1; `role`: `main`; `layer`: actual layer ID.
- `position`, `rows`, `experts`, `top_k`, `hidden_size`, `vocab`: integers.
- `tokens`: exact full input prefix through `position + rows`, with token IDs
  checked against the recorded vocabulary. No position-only trace matching.
- `identity`: SHA256 values for `model`, `weights`, `binary`, `tokenizer`, and
  `source`. The capture driver must derive these from actual files/payloads and
  its source manifest; JSON alone does not prove execution provenance.
- `files`: exactly `hidden`, `logits`, `ids`, `weights`, `output`. Each descriptor
  contains a relative `path` and SHA256 `sha256`. Paths/symlinks cannot escape the
  record directory. All payloads are row-major, little-endian, exact-size:
  hidden/output BF16 `[rows, hidden_size]`, logits BF16 `[rows, experts]`, IDs
  int32 and weights FP32 `[rows, top_k]`.

IDs retain original slot order; duplicate/out-of-range IDs, missing/extra bytes,
invalid hashes, nonfinite values, invalid weights, duplicate JSON fields and
incomplete prefixes fail. The parser limits manifest and payload sizes.

`CaptureContext` owns one explicitly supplied prefix and invocation shape.
It observes the actual layer IDs passed by the decoder; call count never selects
the layer. Duplicate/missing layers, invalid shapes or a nested invocation fail.
Publication requires selected-layer capture completion and successful completion
of the entire model invocation. The GPU wrapper must separately enforce stream
completion and preserve its buffers; the host context does not prove that work
completed on a device.

## Offline comparison

```bash
python3 -B tools/verify/moe_replay_real/report.py \
  .q4t-work/example/t1/record.json .q4t-work/example/t4/record.json \
  --output .q4t-work/example/comparison.json
```

The reader verifies files before aligning overlapping logical positions. Every
compared row requires the same causal token prefix and model/weights/tokenizer
identities. Source and execution binary differences remain visible in the
report. Different future suffixes are not silently treated as past input changes.

The report separately lists hidden input and full-logit differences, ordered
slot changes, expert-set changes, common-expert logits/weights aligned by expert
ID, and MoE output differences. Missing experts are not imputed as zero weights.
Statistics include bitwise count, max absolute difference, L2 and reference L2;
relative L2 is null for a zero reference norm. All values must be finite.

If hidden input already differs, this is an upstream contribution, not proof of
a local MoE shape defect. Even with identical input, the report does not assign
causality among selection, current weights, routed experts and shared experts.
No weight freezing, tolerance acceptance or full-model quality claim is made.

## Host tests

```bash
python3 -B tools/verify/moe_replay_real/test_report.py -v
g++-14 -std=c++23 -Wall -Wextra -Werror -Iinclude \
  tools/verify/moe_replay_real/context_test.cpp tests/test_main.cpp \
  -o .q4t-work/moe-replay-real-20261019/context-test
.q4t-work/moe-replay-real-20261019/context-test
```

Create the output directory first if it is absent. Test fixtures are synthetic
and stored only in `.q4t-work/`. No model loading or GPU calls occur.
