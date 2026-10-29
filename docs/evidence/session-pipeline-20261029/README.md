# Session pipeline evidence — 2026-10-29

Decision: **NO_GO**, complete measurements and correctness checks, no deployment.
See [the report](../../SESSION_PIPELINE_RESULT_2026-10-29.md) and
[the frozen plan](../../SESSION_PIPELINE_PLAN_2026-10-29.md).

- `decision.json`: all four transaction comparisons, main-save and restore gates.
- `final-audit.json`: 632 run seals, unchanged default identities, process cleanup,
  exact write volume, timings, and TTFT; 40 HTTP and 8 overwritten-slot restores.
- `cpu-contract.json`: 68 existing plus 44 new pipeline checks; exact raw-log SHA.
- `resources-summary.json`: physical RAM observations, sampler and exit barrier.
- `reuse-audit.json`: 163 unchanged runtime files; A112/S32 and state-contract reuse.
- `identity.json` / `budget.json`: final candidate, plan identity, and hard limits.
- `entry-repair.json`: initial missing-pynvml launcher ENV_ABORT before any child
  driver/model/request; original records retained separately, system Python used
  for the completed measurement. It is not a failed performance sample removed
  from the experiment.
- `archive-receipt.json`: 1397 files, 512310668 bytes, verified independent copy.

Complete raw records: `.q4t-work/session-pipeline-20261029`.
Independent archive: `.q4t-work/session-pipeline-20261029-archive`.
Its `manifest.json` binds every copied file and all 722 final input identities;
`raw/preflight-entry-failure` and `raw/lease` preserve the first entry failure,
while `raw/lease-execution` is the completed measurement lease.
No logs were discarded for exceeding a size gate. The installed default and
its rollback copy remain 6f3722… / metadata 5fdca8… . Research candidate 72b81a…
is not accepted or installed. No further requests are authorized by this plan.
