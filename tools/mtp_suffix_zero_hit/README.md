# Fixed k3 suffix zero-hit diagnosis

This tool runs the frozen 26-request / six-start diagnostic described in
[the decision card](../../docs/MTP_SUFFIX_ZERO_HIT_2026-10-24.md).
It reuses the existing min32 binary and evalscope collector. It does not change
the admission rule or approve a runtime candidate.

The first HTTP request has native MTP timing disabled. Only after it passes do
five instrumented services run: subset OFF / ON / OFF, then full ON / OFF.
All fixtures are previously observed development material. The two subset OFF
services are an A/A control, not an automatically acceptable noise tolerance.
Read-only telemetry records its own cost and source availability. The full
stage must run under the shared GPU guard; any monitor failure stops owned
processes and remains an environment abort.
CPU/IO samples are periodic 1 Hz observations, not exact request-boundary
snapshots. Short events and exited threads may be missed.

Preparation reads files and discovers telemetry paths; it neither executes a
model nor samples hardware values:

```bash
python3 -B tools/mtp_suffix_zero_hit/prepare_diagnostic.py \
  --stage .q4t-work/suffix-zero-hit-20261024/diagnostic-v1

python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/suffix-zero-hit-20261024/diagnostic-v1/lease -- \
  python3 -B tools/mtp_suffix_zero_hit/run_diagnostic.py \
  --plan .q4t-work/suffix-zero-hit-20261024/diagnostic-v1/frozen-plan.json
```

Do not rerun or overwrite an existing stage. The original program, subset,
fixtures, output reference hashes, tools, model metadata and source snapshot
are bound by the new plan. Previous raw evidence remains unchanged.
