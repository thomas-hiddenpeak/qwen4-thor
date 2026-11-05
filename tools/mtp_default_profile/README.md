# Accepted-default verify timing observation

This is a bounded diagnostic, not an optimization acceptance driver. It runs
the exact accepted ELF and verifies its runtime-source manifest against main;
the containing research checkout's runtime candidate is never executed.

The frozen plan is `docs/DEFAULT_VERIFY_PROFILE_PLAN_2026-11-06.md`: two heads,
two contexts, normal/coarse/fine observation modes, six starts and twelve
requests. It reuses the checked evalscope client, lifecycle audit, telemetry
finalization and shared GPU lease. No profiler dependency changes or retries
are part of the plan.

After committing the driver, `prepare.py --original-root ABSOLUTE_MAIN_ROOT`
creates an exclusive output directory and an absolute deadline. Run `stage.py`
with that plan under **main's absolute `tools/evalscope/gpu_exclusive.py` path**.
Both the stage script and plan arguments must be absolute: the guard starts its
child from the original repository, not the caller's working directory.

`analyze.py --plan ABSOLUTE_PLAN --lease ABSOLUTE_SUCCESSFUL_LEASE --output NEW_JSON`
is offline only. It checks the final monitor and lease, verifies raw seals and
inputs, independently reconstructs responses from the local SQLite files, and
binds timing records to HTTP request IDs. Output must be outside the sealed
`run/` directory. `python -B -m unittest discover -s tools/mtp_default_profile
-p test_analyze.py -v` checks missing event clocks and request-binding failures.

Nested detail totals are never added to their parents. CUDA event intervals
are elapsed stream time, potentially including host gaps and observer work;
they do not establish kernel-only occupancy, bandwidth or GPU idle time. The
fine group deliberately leaves `Q4T_LAYER_TIMING` disabled because its head
interval would include sub-timer reporting and event destruction.
