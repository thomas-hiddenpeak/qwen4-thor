# Main MoE fixed-ID diagnostic

This standalone tool runs the actual layer-2 MoE implementation and weights.
It retains BF16 gating, replaces only expert selection, and computes FP32
selected-logit softmax in the supplied slot order. `serve` and its observer
collector do not enable replay.

The supplied fixture is a **synthetic layer-boundary input**, not hidden states
from a natural-language prefix. It fixes four token IDs as fixture metadata and
four BF16 rows generated with specified unsigned integer arithmetic. This tests
real layer arithmetic; it does not validate full-model quality or reproduce a
free-generation decode trace. Old trace positions alone are never used as token
identity. The first CLI supports this fixed layer/shape fixture; the library
interface supports other validated layer-boundary records.

## Build and run

From the repository root, with model files read-only:

```bash
cmake -S . -B .q4t-work/moe-replay/build \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_COMPILER=g++-14 \
  -DCMAKE_CUDA_HOST_COMPILER=g++-14 -DQ4T_CUDA_ARCHITECTURES=110a
cmake --build .q4t-work/moe-replay/build \
  --target q4t_moe_replay q4t_moe_replay_host_tests --parallel 8
Q4T_REPLAY_TEST_OUTPUT=.q4t-work/moe-replay/host \
  .q4t-work/moe-replay/build/q4t_moe_replay_host_tests
python3 tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/moe-replay/lease -- \
  python3 tools/verify/moe_replay/run.py \
  --binary .q4t-work/moe-replay/build/q4t_moe_replay \
  --model /home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream \
  --fixture tools/verify/moe_replay/fixture.json \
  --output .q4t-work/moe-replay/results
```

Output directories must be new and under `build/` or `.q4t-work/`. The driver
retains the executable, fixture and source snapshots, command/environment identity, complete
logs, raw outputs, and file hashes. Any failed contract stops the run. Check both
`results/identity.json` and the GPU guard's `lease/exit.json`. Both processes
read the frozen fixture copy. Snapshot corruption stops further execution;
editing the original fixture during the run also produces a failure. Preparation
errors are recorded as `FAIL`, and Python `KeyboardInterrupt` as `INTERRUPTED`.
Forced termination that cannot run cleanup still requires the guard exit record;
never accept a leftover `PREPARING` or `RUNNING` status as success.

Host-only driver regression (uses a fake executable, no model or GPU):

```bash
python3 -B tools/verify/moe_replay/test_driver.py -v
```

The suite compares production normal routing, diagnostic normal routing, and
same-shape replay for T=4 and each of four T=1 rows. IDs, logits, weights and MoE
output must match bit for bit. T4 IDs are then explicitly sliced by fixture row
and used with each T1 identity; cross-shape differences are diagnostic counts,
not an acceptance tolerance. Original normal results remain alongside them.
It also checks current-score semantics against independent double-precision CPU
softmax, ties/near-ties, IDs 0/511, rejection sentinels and scratch reuse.

## Replay a saved route

```bash
python3 tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/moe-replay/replay-lease -- \
  .q4t-work/moe-replay/results/executable replay \
  /home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream \
  tools/verify/moe_replay/fixture.json \
  .q4t-work/moe-replay/replayed \
  .q4t-work/moe-replay/results/suite/t4-row0.route
```

The executable is part of the identity. For a deliberate cross-version
experiment, append **the exact source executable SHA256** and **a nonempty
allowed-difference explanation**. The execution SHA is obtained from the current
executable, and both hashes plus the explanation are saved in
`compatibility.txt`. This exception permits only the binary difference: model,
actual layer-weight payload hashes, input hidden bytes, layer, tokens, positions
and shape must still match. Do not approve arbitrary changes just to make an
input pass. FP8 experimental settings are rejected before loading the layer.

## Supplying explicit ordered IDs

C++ callers use `MoEReplayRecord::Prepare(source, execution, ids, compatibility)`
then `WriteMoEReplay(path, record)` to produce a file accepted by the CLI.
The IDs are original expert numbers, row-major `[rows, top_k]`; they are not
sorted or deduplicated. Duplicate/out-of-range IDs and incomplete rows fail.
`MoEReplayForward` accepts that validated record, recomputes gating, verifies the
actual hidden digest and drains the caller's stream before return (also on
failure). It owns an ID snapshot through completion; external CUDA buffers must
remain valid during the call. The loader binds model/weight provenance; the
low-level API cannot infer it from raw device pointers. Callers must not mutate
the source record concurrently while it is being copied.

The version-1 route format is bounded to 16 MiB, little-endian, with a SHA256
checksum. It includes source binary identity, model/weights/hidden identities,
explicit role, layer, starting position, rows, expert count, top-k, full token
prefix and ordered IDs. It rejects unknown versions/roles, incomplete or trailing
payloads and checksum failures. A checksum detects corruption, not authenticity;
execution identity validation remains mandatory.

## Interpretation and limits

Fixed IDs isolate discrete expert selection. Residual differences may still
come from current routing weights or expert arithmetic; inspect saved logits,
weights and outputs before attributing them. This primitive does not freeze
weights or bypass gating. A separate full weight-freezing ablation is outside
scope. T1/T4 agreement on this fixture does not establish batch invariance.

No draft MTP routes, trace-token reconstruction, expert offload, residency bias,
prefetch or CUDA Graph are implemented. Default service behavior and existing
N=4 numerical contracts remain unchanged. The diagnostic has synchronization and
readback overhead and must not be used to claim serving performance.

Results and identities: [A1 report](../../../docs/MOE_REPLAY_A1_2026-10-19.md).
