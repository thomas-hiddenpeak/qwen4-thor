# Bounded session snapshot pipeline experiment

Result: **NO_GO**, not a delivered runtime optimization. See
[the result](../../docs/SESSION_PIPELINE_RESULT_2026-10-29.md) and
[the frozen plan](../../docs/SESSION_PIPELINE_PLAN_2026-10-29.md).

The one candidate used two 8 MiB buffers and one joined writer, with
`Q4T_SESSION_PIPELINE=1`, `Q4T_SESSION_PREALLOCATE=0`, and
`Q4T_SESSION_SAVE_TIMING=1`. It preserved file layout, complete state, and
publication after writes and successful HTTP completion. The installed binary
was not replaced. Do not enable this research candidate in the agent launch
examples.

`prepare.py` freezes identities and the fixed 40-request ledger. `run_stage.py`
must run under the shared GPU guard using the system Python that has pynvml;
evalscope clients use their own existing venv. The stage is already exhausted;
do not rerun its plan or extend its original deadline. `storage_contract.cpp`
adds deterministic in-flight failure/cancellation checks to the existing CPU
storage contracts. No fault injection is linked into the inference program.

`analyze.py` and `archive.py` read the stopped evidence only. The archive tool
creates a new directory and intentionally refuses to overwrite an old archive.
All raw logs are preserved without a log-size rejection gate. The initial
preflight interpreter error remains separate from the completed measurement.
