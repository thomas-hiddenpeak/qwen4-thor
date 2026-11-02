# SRV-06 text stop acceptance

Frozen contract: [scope and limits](../../docs/SERVE_TEXT_STOP_2026-11-03.md).
No import starts a listener or model. `test_stage.py` and `check_schema.py` are
CPU-only driver/schema controls. `verify.py` runs only through the guarded stage.

`stage.py prepare` requires clean committed sources, successful CPU evidence,
the known installed pair and a warning-free production build. It freezes all
inputs, the previous pair and source bundle before any GPU acceptance. Run
`execute` under an absolute-path GPU guard output ending in `execution/lease`;
`finalize` requires its successful guard receipt. `rollback --output ABS_DIR`
requires a new guard and restores only the identity-bound owned pair.

Budget: 21 planned generations, 16 input negatives, 3 starts; ceilings
22/16/4 and 1800 seconds from first execution. No automatic retry or reset.
Active stop with constraints/state caches is rejected; neutral stop retains the
previous behavior. Ordinary auto tools must finish parsing before execution.
Original failed compiler/fixture records are retained separately; only the
affected fixture is rerun. Neither commit nor CPU success is release acceptance.

The first relative guard invocation was rejected before model/request work.
Use `stage.py launch --guard-retry` for the frozen absolute-path continuation;
`prepare/finalize/rollback` also take `--guard-retry`. Its original deadline
comes from the first failed lease, which remains intact. Runtime is unchanged.

The system-Python attempt then failed at SDK import after one cleanly closed
model start and zero requests. The final continuation is `--sdk-retry`, with
`launch` binding the already accepted SDK venv and constructing its real client
before taking the GPU lease. That prior start and the original deadline remain
charged. Use the same continuation flag for prepare/finalize/rollback.

The concrete length-fixture correction uses `--fixture-retry`. Seven accepted
responses and sixteen negatives are reused by hash; 11 MTP requests remain.
Plain JSON/SSE and installed health share one real installed --no-mtp process
(3 requests), keeping totals 22 generations / 16 negatives / 4 model starts.
Installed MTP uses identical candidate-ELF evidence; no extra MTP load is claimed.
