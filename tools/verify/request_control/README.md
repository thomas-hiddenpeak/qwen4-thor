# Request cancellation verification

Contract and bounded delivery report: [request cancellation](../../../docs/REQUEST_CANCELLATION_2026-09-27.md).

Host defect tests (use a fresh output directory):

```sh
python3 tools/verify/request_control/run.py --output .q4t-work/request-control-unit-new
python3 tools/verify/request_control/check_connection.py --output .q4t-work/request-eof-new
```

`check.cpp` exercises registration, credentials, reuse, deadlines, shutdown and
2,000 completion/cancellation races. This is not a thread-sanitizer proof.
`check_connection.py` extracts the production transport predicate and checks
live TCP, write-half-close and RST on loopback.

Full-model defect scenarios use `tools/evalscope/run_request_cancellation.py`;
`run_accept_failure.py` uses `accept_failure.cpp` as an LD_PRELOAD injector.
The latter expects service exit 1 after safe cleanup, while its test driver
returns 0. Readback injection uses `run_state_readback_failure.py --binary`.
These synthetic errors are not evidence of real GPU fault recovery.
Use each tool's `--help` for required evidence paths. Quality and performance
use the existing `run_acceptance.py`; do not replace E2E with these unit tests.

`seal.py` is specific to the 2026-09-27 final delivery directories. Run once,
only after the serial driver and all writer processes terminate. Its stdout
must be outside the sealed directories. It verifies candidate identity,
source snapshots, exits and 26 raw evalscope rows, then hashes the evidence.
Do not rerun it over a sealed experiment or overwrite historical evidence.
