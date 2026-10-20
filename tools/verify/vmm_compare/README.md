# Actual-model VMM comparison

Research linkage only; production `build/q4t` is never replaced.
Scope and results: `docs/VMM_LIFECYCLE_COMPARE_2026-10-20.md`.
All generated binaries/evidence live in `.q4t-work/vmm-compare-20261020`.

- `build.py`: exact accepted-object relink identity gate, independent runtime and
  VMM executables and linked-contract executables. Requires retained foundation
  build objects and local CUDA 13.3. No GPU execution.
- `memory_trace.cpp`: application CUDA calls with begin/end, pointers, stream,
  flags, thread and return codes. Library-internal calls are outside this scope.
  VMM replaces synchronous allocations >=32MiB, synchronizes before unmapping,
  and aborts if such a pointer reaches the unsupported async-free path.
- `cache_start.py`: targeted zero-residency prerequisite, explicit full-file
  warming, exact per-file payload vector gate before launch. No global cache
  dropping. Historical one-pass repair is bounded and was unsuccessful.
- `run_arm.py`: one complete model startup, fixed 1K request, and cleanup.
- `run_pair.py`: holds an inherited exclusive lease throughout a fixed pair;
  each arm retains NVML monitoring and failure evidence. It stops on non-NVML
  failures. A declared alternate arm is not a retry of an interrupted arm.
- `trace_audit.py`: actual application allocation/free ledger. Async-free success
  is recorded as submission, not proof of physical reclamation.
- `audit.py`: read-only verification of all sealed stages and equality checks.

Build and audit from the repository root:

```sh
python3 -B tools/verify/vmm_compare/build.py
python3 -B tools/verify/vmm_compare/test_cache_start.py
python3 -B tools/verify/vmm_compare/test_trace_audit.py
python3 -B tools/verify/vmm_compare/audit.py
```

GPU budget is exhausted; do not rerun into the sealed stage directories.
Completed experiment commands and the justified budget revision are retained
in the report and stage plans. None of these tools grants deployment approval.
The source wrappers retain exact call identities and physical mapping ownership;
this is not a generic allocator for arbitrary async-free/context combinations.
