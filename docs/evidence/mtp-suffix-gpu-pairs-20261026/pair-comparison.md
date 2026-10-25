# Recorded GPU pair extraction

INCOMPLETE

Observed HTTP response rows: 1; qualified captures: 0. No GPU timing or OFF/ON conclusion can be extracted.

Original strict judgments and unknown memset parameters remain unchanged. This is an instrumented single-pair description, pending independent stage audit.

| Pair | OFF strict | ON strict | Unknown memset OFF / ON | Pair status |
|---|---|---|---|---|
| 8K |  |  |  /  | INCOMPLETE |
| 200K |  |  |  /  | INCOMPLETE |

## Limits

- One OFF/ON pair per context is descriptive under instrumentation, not normal-performance acceptance or statistical noninferiority.
- Recorded GPU union excludes unobserved work; gaps between recorded activity do not prove device idle.
- Phase unions overlap across groups and must not be added; duration sums are work sums, not elapsed time.
- Host wall minus thread CPU is non-running wall, not GPU wait. API duration includes driver work and possible blocking.
- Nested host inclusive phases overlap. Only explicitly exclusive host values may be summed as work, and threads may still overlap.
- Missing cudaMemsetAsync parameters remain unknown in each capture; no historical or cross-process parameter/correlation join is performed.
- First/later generation groups follow native MTP round order, not an exact client TTFT boundary.
- Original strict FAIL/UNKNOWN judgments remain intact. Limited qualification does not certify whole-trace integrity.
- Consumed JSON seal checks do not replace independent raw/ownership/monitor/final-NVLOG reconciliation audit.
