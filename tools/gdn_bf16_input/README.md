# GDN BF16 input feasibility

Scope and fixed budgets: [plan](../../docs/GDN_BF16_INPUT_PLAN_2026-11-06.md).
`prepare.py --original-root ROOT --build BUILD` binds the accepted default,
three-file candidate delta, build records, existing inputs and dependencies.
Commit the source first. Artifacts remain under `.q4t-work/gdn-bf16-input-20261106`.

Run the entire `stage.py --plan ABSOLUTE_PLAN` through the **original root's**
absolute `tools/evalscope/gpu_exclusive.py --output ABSOLUTE_LEASE -- ...`.
Use absolute script paths: the guard starts its child in the original root.
After the lease completes, `stage.py --plan PLAN --analyze-only --lease LEASE`
checks final monitoring, seals, identities, request trajectories and all four
investment gates. It does not accept performance or authorize deployment.

Reuse the existing single-slot HTTP driver and final-monitor adapter. No model,
GPU tests, warmups, retries, benchmarks or profiling precede the HTTP gate.
