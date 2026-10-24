# Monitor rerun read-only review — 2026-10-24

Review: **PASS_WITH_SCOPED_LIMITS**. Existing CPU/native proofs and production-monitor records close within their scopes; stage validity remains **ENV_ABORT**. No test, process scan, hardware sample, HTTP or GPU work was run for this review.

- Stage plan: `f6951b0c65b2b3918ca0d2696c14b8a724e0c5e10dae4321ac0ffdac8b7e536b`.
- Raw seal: 68/68 files match before and after; 83 selected raw/proof inputs are unchanged.
- CPU: 97/97 methods, one suite, no failures/errors/skips; suite 0.856793316 s, driver 1.065499814 s. All 15 frozen source originals/copies and 20 proof dependencies match.
- Native: 64/64 children completed and were reaped in 0.217599096 s; 32 one-thread then 32 eight-thread, zero forced cleanup or cleanup errors.

| Native scenario | Children | Pending admitted/resolved | First non-OK observations |
|---|---:|---:|---|
| One thread | 32 | 15/15 | {"TERMINATED_AFTER_IO_DENIAL": 17, "EXITING_IO_UNAVAILABLE": 15} |
| Eight threads | 32 | 0/0 | {"TERMINATED_AFTER_IO_DENIAL": 6, "EXITED_DURING_IO_READ": 26} |

All 15 pending admissions retain matching PID/start ticks, R/R initial and recheck states, PF_EXITING, vsize 0, Kthread 0, no Vm fields, io EACCES, and a prior nonready pidfd. All 15 later have ready pidfd plus same-identity Z; pending ledgers end empty. Error-to-confirmation min/median/max: **0.530939/0.571171/0.625392 ms**. Raw integer nanoseconds are retained per case in JSON.

The native driver calls production read/ledger functions in a tight loop and resolves immediately; its reap loop sleeps 1 ms only when needed. These timings are **not production 1 Hz sampler latency**. All pending cases are single-thread; the eight-thread group supplies no pending coverage.

Production monitoring completed **11/11** flushed rows across 10.025371838 s, with 53 sources per row (**583 source rows**, all OK). Its 41 process observations are **39 OK + 2 EXITED_DURING_IO_READ**. The latter retain missing io and same-identity Z evidence; no io zeros are fabricated. There were no q4t thread observations because no model ran. Pending admitted/resolved/polls and pidfd waits are all **0**. Thus this CUDA-fixture stage supplies **no pending-branch coverage**.

The failed-step barrier is complete: cleanup boundary 998698.959597158, full sample start 998699.488234941, flush 998699.507278995. That is 528.637783 ms to sample start and 547.681837 ms to flush, with zero pending. Its scope is post-owned-step-PGID cleanup, not all detached descendants. The normal success barrier is empty and was not exercised.

The raw driver status is STOPPED; postflight is ENV_ABORT because **direct-external required forced cleanup**. These are compatible: monitoring closed cleanly while collector cleanup failed. Zero model loads/HTTP requests ran, so this stage supplies no 8K/200K GPU comparison or performance acceptance.

Actual monitor/native/runner sources match CPU freeze, CPU proof, stage plan and stage snapshot:

| Source | SHA256 |
|---|---|
| telemetry.py | `f93f79314d3cbf29bc3062994af7ea1b343e9fb225241c9b45e84775813d6b1a` |
| reproduce_exit_pending.py | `68f98767192134081a3645d85923501a13c98f915aaf4a2dba9f757ab6ea9997` |
| run_stage.py | `a9dbc5a03dde5e6608f1c2cf6f0c862b16f4fd7fe0620153d3ca7394102f40fc` |
| prepare_rerun.py | `ddc0294bf354dd5d1b1a6cff9a13e47554174771c93f1be3e29435148037300b` |

Kernel header receipt `954bf3320ffab0ee42abab65bd28e5894db577630e597324aa139e0b77145e54` binds PF_EXITING=4 and identical original/copy hashes. This supports the flag definition, not the cause of EACCES. Polling cannot exclude sub-interval interference; the independent stage/lease audit handles detached-process cleanup.

JSON analysis SHA256: `4453425b129a712ee0a0dae944d56c77238251d4302b35a37518b48f20400c08`.
Review errors: [].

V2 correction: v1's log parser missed the one unittest method whose docstring placed `... ok` on the following line. The exact two-line outcome, all 97 method identities, the unittest summary and proof/result/exit receipts now agree. Counts by module are 39/25/14/11/8. V1 JSON/MD remain unchanged; all 83 raw/proof inputs and all CPU source identities were rechecked unchanged. No tests or measurements were rerun.
