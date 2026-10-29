# Bounded private session incremental snapshot experiment

Result: **LOCAL_GO_REQUIRES_DELIVERY_REVIEW**, not an installed release. See
[the result](../../docs/SESSION_INCREMENTAL_RESULT_2026-10-29.md),
[the original contract](../../docs/SESSION_INCREMENTAL_PLAN_2026-10-29.md), and
[the wiring repair and budget deviation](../../docs/SESSION_INCREMENTAL_CONTINUATION_2026-10-29.md).

The single candidate reduced resave writes by 96.97% in both heads. Including
initial saves, the fixed lifecycle HTTP throughput improved 6.40% / 14.09%.
Initial full-head transactions regressed 3.90% / 0.31%; those observations and
the original failed phase remain recorded. The installed executable is unchanged.

`Q4T_SESSION_INCREMENTAL=1` selects private sparse block manifests and skips
writes only for exactly matching lengths and SHA256 after a complete restore
in the current lease. All state is still copied and hashed. The public pool,
model math and default-off storage path are unchanged. This is research, not
a deployment recipe; PREALLOCATE and PIPELINE were explicitly disabled.

The completed ledger is 48 valid HTTP requests including 16 overwritten-slot
restores, plus 5 wrongly configured requests (4 success, 1 cancellation),
5 starts and two CPU programs: 205 storage checks and 2 tool tests (7 subcases).
The original four-start/48-request stop policy was amended once for the tool
wiring error. Deadline and acceptance gates were unchanged. Do not rerun or
extend either expired plan.

`prepare.py`/`run_stage.py` retain the original experiment entrypoints;
`prepare_continuation.py` froze only the remaining three services after the
original inputs and failed run were independently archived. Use system Python
for the shared exclusive guard and complete driver; evalscope uses its venv.
The CPU-only fault wrappers are never linked into the inference binary.

For offline reproduction, `review.py` executes the sealed
`analyze_continuation.py` with one recorded schema correction: the resource
summary key is `memory`, not `overall`. It preserves the original file and
emits the corrected source and both hashes. No gate or runtime input changes.
`archive.py` preserves the original failed phase; `archive_continuation.py`
preserves the valid continuation, its reused baseline and all raw output.
Neither archive imposes a log-size rejection gate or overwrites an old archive.
