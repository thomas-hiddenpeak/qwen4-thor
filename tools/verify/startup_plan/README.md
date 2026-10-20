# Configuration-derived startup reservation

This is an explicitly enabled reliability candidate, not the default allocator.
Read `docs/STARTUP_RESOURCE_PLAN_2026-10-20.md` for the frozen scope, failures and
acceptance status. It supports the current text architecture only.

`BuildStartupPlan` inspects configuration and tensor metadata without reading
weight payloads. The candidate reserves each device block before pinned loading,
then checks the actual logical allocation sequence, sizes, pointer ownership and
ready boundary. Default MTP scratch/checkpoints are included. The physical order
is a stable descending-byte permutation; logical handoff order remains unchanged.
The serialized plan and its SHA256 bind both orders.

Build independently (never overwrite a sealed build manifest):

```bash
python -B tools/verify/startup_plan/build.py \
  --output .q4t-work/startup-plan-20261020/order-candidate
```

The `Q4T_STARTUP_RESOURCE_PLAN` CMake option defaults OFF. The wrapper is linked
only into an ON research executable. `Q4T_STARTUP_PLAN_ONLY=1` exits before CUDA
and payload reads; `Q4T_STARTUP_PLAN_OUTPUT` must name a new file.

Host tools:

- `test_plan.py`: real read-only metadata and effective configuration boundaries;
  `--base` selects a sealed candidate directory.
- `test_core.py`: checked shape arithmetic and malformed tensors, with project
  GPU APIs and payload reads forbidden; accepts `--build-manifest` and `--output`.
- `test_wrapper.py`: the actual wrapper with fake CUDA, ownership/order failures
  and rollback; requires a new `--output` directory inside `.q4t-work`.
- `test_run.py`: preparation, configuration and evidence gates without GPU work.
- `tools/verify/device_reserve/test_owner.cpp`: shared reservation owner contract.

`run.py` is the frozen real-environment driver. It holds the shared GPU lease
through preparation, loading, HTTP and cleanup, preserves exact full-hot file
residency at launch, and retains the original NVML five-second query deadline.
Only the three named order-candidate stages are enabled. Run each once, in order;
any failure stops the matrix. No automatic retry or performance acceptance:

```bash
python -B tools/verify/startup_plan/run.py --stage mtp-order-v1
python -B tools/verify/startup_plan/run.py --stage restart-order-v1
python -B tools/verify/startup_plan/run.py --stage boundary-order-v1
```

An ENV_ABORT cannot be accepted using a late successful NVML result. An absent
scope proves process cleanup, not application-level frees or physical RAM return.
Earlier runs retain their own binary/source identity; their successful results do
not automatically accept a revised reservation order.

`analyze.py --stage <sealed-stage> --output <new-json>` performs offline kernel/
NVML attribution. `audit.py --partial --output <new-json>` checks complete
research evidence without GPU activity; `--finalize` writes an exclusive final
receipt, including a negative/incomplete engineering decision. An audit report
whose evidence is VERIFIED does not imply runtime acceptance: check its separate
`runtime_acceptance` and per-stage status fields.
