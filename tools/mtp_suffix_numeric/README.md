# Numeric-format suffix admission: one bounded candidate

Completed 2026-10-24: **NO_GO_PROTECTION_REGRESSION**. All 238 requests and 18
starts completed within 9676 seconds. All repeated-code/text goals passed for
both heads, but old 8K retained one extra ordinary-tail forward and held-out
45K regressed in both startup pairs and both heads. Quality, direct contracts
and cancellation/recovery passed. The candidate is archived; installed f356
and its release metadata remain unchanged. This does not authorize a rerun or
additional parameter search. The frozen card, results and archive receipt are in
[the numeric-admission plan](../../docs/MTP_SUFFIX_NUMERIC_GATE_2026-10-23.md).

The following describes the completed experiment. It did not rerun or modify
the original suffix candidate's sealed NO_GO stage.

The main budget is **238 inference HTTP requests, 18 HTTP service startups,
14400 seconds**, including at most 1200 seconds for direct contracts. No warmup,
retry, parameter scan, or automatic sample increase is allowed. If accepted,
deployment has a separate ceiling of three startups/three inference health
requests, for at most 241 total. Health/cancellation control calls are recorded
separately and do not generate tokens.

Execution order:

1. Full-head suffix ON, the existing 11 exact-retrieval quality cases. This is the
   first test. Compilation, static review, fixture construction and identity
   freezing are preparation; no host test or profile precedes this HTTP gate.
2. Frozen new direct contracts, then subset suffix ON quality (11 requests).
3. Two startup-level paired blocks per head: full OFF→ON then ON→OFF; subset
   ON→OFF then OFF→ON. Each of the eight services runs exactly 22 requests:
   five new context tiers × three requests (15), four new scenarios × one (4),
   old 8K × one, old repeated code/text × one each (2).
4. Installed parent plain decode, then candidate plain decode: the same new five
   tiers × three requests per service (30 requests total). The candidate's
   prompts/outputs must match the contemporary parent's results.
5. Full/subset explicit cancellation, each control/cancel/fresh recovery (6).
   This uses old repeated-code material; it is a lifecycle contract.
6. Full OFF/ON and subset ON/OFF, one old-8K request each with Q4T_MTP_TIMING (4).
   These four services are diagnostic only and never throughput samples.

The sum is 22 + 176 + 30 + 6 + 4 = 238. CPU contracts are supplied explicitly;
old GPU state and ordinary quality evidence may be reused only through a
reviewed file/dependency audit. Completion is MEASURED_REQUIRES_REVIEW, not an
acceptance or deployment instruction.

New acceptance and old development data remain separate. The five new tiers
use new record templates and seed 202610230731, snapped to complete records in
[nominal−48, nominal]. New repeated code contains numeric constants and numeric
expressions; new prose concerns conservation work; the nonrepeat task concerns
a music school; the numeric-enumeration control uses a separate approximately
8K ledger and seed 202610230732. A scenario name does not promise zero hits or
establish semantic correctness. All prompts are generated and hashed before
observing candidate output. Old 8K/code/text are copied byte-for-byte at the
prompt level, with source hashes, and labelled development_regression. They are
not presented as unseen acceptance material.

Goals remain head-specific. For each of new/old code/text, the geometric mean
of the two paired ON/OFF decode ratios must reach 1.05 and each block must be
positive. Old 8K must show no decode decline in either block, nonnegative
aggregate change, and no increase in verify or ordinary-tail work. The eight
previously diagnosed numeric mistakes are checked by the separate CPU replay;
new runtime step order need not produce exactly eight rejected queries. Every
new tier, TTFT, nonrepeat/numeric control, ordinary decode, quality and lifecycle
protection is reviewed separately. There is no universal loss tolerance, and
credible regression cannot be averaged away. Two paired startup blocks still
do not establish reliable population noninferiority or eliminate inherited
cache/time correlations. Keep every request, startup's first request, block and
arm order; do not treat three requests within one startup as three startups.

The driver imports the original suffix driver's unchanged guard, sampler,
process cleanup, lifecycle and outer execution loop. A local audit callback adds
required numeric-class-table initialization/release, exact vocabulary/capacity,
per-request admission counters, positive index allocation and explicit MTP
fallback checks. It rejects an ungated old binary even if it accepts the same
CLI. `numeric_ns` is **inside `query_ns`**, so it is never added a second time.
Shared table startup/release cost and its 248320 bytes are separate from each
request's bounded index. Both occupy unified physical RAM; RSS, NVML and system
memory/cache peaks overlap and must not be summed.

Each custom service uses the same evalscope HTTP collector as the standard
runner. All server/client descendants belong to its outer step process group.
Only that owned group is terminated on failure. One outer gpu_exclusive lease
covers the entire stage and gaps. Its NVML monitor and the one-second RAM
sampler remain active; sampler age above five seconds or failure to join within
five seconds is ENV_ABORT. Cache policy is existing startup auto with naturally
inherited page cache: no drop_caches, manual preload, warmup or SSD-cold claim.
The local lifecycle 90/120-second waits are failure triggers; executor cleanup
can wait for the reader, so the 900-second lifecycle-step deadline and four-hour
stage deadline are the hard bounds. First HTTP/quality/contract/identity/timeout
failure stops further steps without retry. Intermediate performance values do
not change the frozen sequence. Missing evidence stays missing.

Prepare only after the candidate build and direct-contract/reuse files exist:

```bash
tools/evalscope/.venv/bin/python -B tools/mtp_suffix_numeric/prepare.py \
  --stage .q4t-work/suffix-numeric-gate-20261023/acceptance-v1 \
  --candidate .q4t-work/suffix-numeric-gate-20261023/build/q4t \
  --contracts .q4t-work/suffix-numeric-gate-20261023/contracts.json \
  --reuse-record .q4t-work/suffix-numeric-gate-20261023/reuse.json \
  --extra-file .q4t-work/suffix-numeric-gate-20261023/build-identity.json

python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/suffix-numeric-gate-20261023/acceptance-v1/lease -- \
  python3 -B tools/mtp_suffix_numeric/run_stage.py \
  --plan .q4t-work/suffix-numeric-gate-20261023/acceptance-v1/frozen-plan.json
```

The destination stage must not exist. Contracts are a JSON list of name,
command array, timeout, and optional files to hash; timeout totals cannot exceed
1200 seconds. Include this new audit suite after the first gate:

```bash
python3 -B -m unittest discover -s tools/mtp_suffix_numeric \
  -p test_stage_audit.py -v
```

Preparation copies parent binary/release/actual CMake cache, builds CPU-only
fixtures, freezes source/helper/build/fixture/subset identities, and snapshots
source including current dirty files. Tokenizer/config and the subset source
shard are content-hashed; other immutable model shards get file metadata, not
a claim of newly computed full-model content hashes. Use --extra-file for build
logs, the exact candidate build receipt, and external evidence dependencies.
The --reuse-record must state exactly which original contracts are reused and
why their dependencies are unchanged.
