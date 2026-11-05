# Consumed extend-head rows

One candidate on top of main def8cc96. `prepare.py` freezes a clean committed
source tree, zero-warning build, accepted parent, existing fixed HTTP inputs
and exact subset artifact. It does not run tests or accept performance.

Run `stage.py --plan PLAN` under the original checkout's `gpu_exclusive.py`
for the complete four-arm phase. The first tests are real evalscope HTTP
requests; there are no warmups, retries, kernel probes or benchmarks.
The shared `monitored_stage` finalizes telemetry before analysis. Raw output
is sealed; the derived decision lives beside it and cannot authorize rollout.

Only same-head parent/candidate responses are compared. Both full and subset
must meet the frozen +3% aggregate short-decode and lower E2E gates before
any further work. A valid NO_GO closes this trial; ENV_ABORT is not NO_GO.
Independent numerical, five-tier, multi-slot and cache acceptance remains
conditional, with the fixed budgets in
[the plan](../../docs/MTP_SELECTED_HEAD_PLAN_2026-11-06.md).
