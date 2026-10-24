# Nsight collector lifecycle controls

This is a bounded tool diagnosis, not an inference benchmark or a replacement
for the suffix performance acceptance matrix. The frozen scope and budgets are
in `docs/MTP_SUFFIX_NSYS_2026-10-24.md`.

`fixture.cu` executes 32 independently checked kernel operations, 32 H2D and
32 D2H transfers, 32 nonzero memsets (512/16384 bytes alternating), and 33 named
NVTX ranges. Its ordinary CUDA allocations occur
before capture. After `cudaProfilerStop`, it waits at most 60 seconds for its
owner's release file and then frees its allocations. The receipt independently
binds every output, CUDA status, PID, TID and normal release.

`fixture_audit.py` checks exact counts, IDs, transfers, context/stream ordering,
and both directions of API/activity correlation. Independent SQLite derivatives
remove a complete launch/kernel pair or the final NVTX range. Their rejection
does not simulate or prove the absence of native buffer loss. Diagnostics stay
separate: known fixture work may be complete despite a warning, but that never
exempts an arbitrary model trace from its integrity gate.

`prepare_stage.py` binds the unchanged f0ff observation binary, previous passed
HTTP/CPU evidence, repaired monitor, new tools, fixture binary and actual Nsight
libraries. `run_stage.py` retains one repository-wide GPU lease through all
four lifecycle controls, conditional model captures and final telemetry flush.
The only candidate is the wrapper with explicit named-session stop. Unresolved
target diagnostics stop the stage; it does not choose a more favorable variant.

The six conditional HTTP requests are subset 8K OFF, 8K ON, 200K ON and 200K OFF;
each 200K service first handles one uncaptured 8K request. All use the existing
evalscope collector and frozen output identities. No repeat-benefit samples,
runtime optimization candidate, deployment or new five-tier claim are added.

`analyze_gpu.py` is offline only. CPU/NVTX identity keys associate the spans;
CUDA execution and API timing stay on the Nsight clock. Thread CPU can include
submission and busy waiting, and observed GPU envelope gaps are not automatically
CPU submission costs. The analysis cannot convert profile time into normal
throughput or infer that old 200K/8K changes have been repaired.

The 2026-10-24 run ended with `ENV_ABORT` after 5.229 seconds: monitoring failed
while the first fixture was exiting. Its known work was complete in the saved
trace, but actual-target CUDA/NVTX integrity warnings remained. Only synthetic
CPU negative controls ran; the real capture negatives, other three variants and
all model captures remain unmeasured. External stop is an unvalidated candidate.
