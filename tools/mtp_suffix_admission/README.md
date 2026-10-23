# Fixed k3 suffix admission: one minimum-match candidate

This stage evaluates one rule: after the existing complete-three, d0 and numeric
gates, admit the selected suffix only when its match length is at least 32.
Lookup still uses the 3–64 token range, four candidates, longest match and newest
tie choice. No alternate candidate, dynamic length, timing adaptation, new serve
option or threshold sweep is introduced. The decision card is
[the admission plan](../../docs/MTP_SUFFIX_ADMISSION_2026-10-24.md).

The previous numeric-only and original suffix stages remain sealed. The
installed f356 binary and explicit 65536-row BF16 subset stay unchanged until a
reviewed acceptance decision. All previously observed material is development
data in this experiment. A completed measurement is not acceptance.

## Frozen execution budget

One candidate, one main stage: **250 inference HTTP requests, 18 service
startups, 14400 seconds**, including at most 1200 seconds of direct contracts.
No retry, warmup, cache clearing, parameter scan or added sample is permitted.
Acceptance alone permits the separate deployment ceiling of three startups and
three inference health requests: at most 253 in total. Readiness, health and
cancellation control calls are recorded separately from inference requests.

The complete driver holds one shared `gpu_exclusive.py` lease throughout:

1. Full-head suffix ON, the existing 11 exact-retrieval quality cases. This is
   the first test. Build, static review, CPU fixture construction and identity
   capture are preparation, not inference or contract tests.
2. Frozen affected host/stage/replay contracts, then subset ON quality (11).
3. Eight main services: full OFF→ON and subset ON→OFF in block 1; full ON→OFF and
   subset OFF→ON in block 2. Each service has exactly 23 requests: five new tiers
   times three requests (15); new repeated code, repeated text, nonrepeat and
   numeric-enumeration controls (4); development old 8K, old 45K and code/text (4).
   Thus the same-head startup pairs provide 184 main requests.
4. Contemporary installed parent plain decode, then candidate plain decode:
   five new tiers times three requests per service, 30 requests total.
5. Full/subset cancellation lifecycle: control, cancellation and fresh recovery
   per head, six requests. The development code fixture is used. Cancellation
   waits for at least 64 nonempty content chunks so the fixed 32-token admission
   can participate; the cancelled request must actually record a suffix hit.
6. Four separate timing services: full OFF→ON and subset ON→OFF. Each runs old
   8K then old 45K once with `Q4T_MTP_TIMING=1`, eight diagnostic requests total.

The sum is 22 + 184 + 30 + 6 + 8 = 250. Each performance request uses greedy,
single sequence, k3, exactly 256 length-finished output tokens, max_prefill=8192
and max_len=208896. The runner checks actual prompt tokens and frozen prompt
hashes. It retains every output hash, raw request, startup first request and
same-head OFF/ON identity for final comparison. Main requests have no GPU timing
instrumentation; diagnostic requests cannot replace acceptance throughput.

## Data and targets

The new five-tier seed is 202610240731. New bodies concern public learning
demonstrations: planetarium alignment, glaze samples, tidal observations,
climbing safety, costume fitting, bicycle braking, tea tasting and aquarium
behavior. Records use a different request and review task. The complete-record
prefix fits each nominal context within [nominal−48, nominal].

New repeated code packs courier orders into boxes with meaningful numeric
constants, state updates and long repeated blocks. New repeated prose describes
a classroom soil/sand experiment. The nonrepeat control concerns a neighborhood
astronomy program. Numeric enumeration uses cedar/birch/maple parcel routes and
seed 202610240732. No scene name promises zero hits or semantic correctness.
Prompts are constructed and hashed before the first output from this candidate;
they cannot be adjusted after observing results.

Development fixtures are copied byte-for-byte at the prompt level:

- `old-8k`: the original suffix stage's 8159-token input.
- `old-45k`: the numeric stage's 45035-token input.
- `old-repeat-code/text`: the numeric stage's then-new code/text inputs, now
  explicitly development material. Earlier repeated fixtures and the numeric
  stage's 1K/8K trajectories belong to CPU development replay, not extra HTTP.

Each head must retain useful gains separately in new/development code and text:
the geometric mean of its two paired ON/OFF decode ratios must be at least 1.05,
and both block ratios must exceed 1. Neither a different scene nor a different
head can compensate for a failed target.

For old 8K and old 45K, every head/block must eliminate the extra target work:
whole-generation target forward calls and processed rows must not increase
relative to its same-block OFF arm. Report verify, ordinary tail, restore,
neural proposal and accepted-prefix extend separately. Calls are verify+tail T1;
rows are 4×verify+tail T1. These counts include the first MTP round and exclude
prefill/init; they do not have the latency−TTFT timing window. Equal target work
does not prove equal throughput.

Decode, TTFT, quality, output identity, ordinary decode, controls and lifecycle
remain separate protections. Review every block/tier/head for credible loss;
there is no universal percentage tolerance, automatic per-block ratio≥1 rule
for development probes, or average that can erase a credible regression. Tiny
negative observations are preserved, not automatically called proven losses.
Two paired starts per head provide descriptive evidence, not reliable population
noninferiority. Within-start requests are not independent startup replicates.

Reuse records must identify exact unchanged source/artifact/build dependencies
and the scope of prior evidence. Unchanged core GPU state oracles, BF16 subset
contracts and ordinary quality may be reused after that audit. New admission
behavior, its counter interpretation and HTTP integration get new evidence.
Old elapsed times never substitute for the present same-configuration pairs.

## Runtime audit, resources and stopping

The new adapter imports the original suffix driver's unchanged outer loop,
resource sampler, guard check and owned-process cleanup. Old drivers are not
edited. Every ON startup must report `numeric_gate=1 min_admitted_match=32` and
one classification-table init/release pair. It rejects the numeric-only binary,
wrong rule, invalid/capacity fallback, missing per-request index allocation and
explicit MTP fallback. Table initialization must precede admitted requests and
release must follow all suffix summaries and request-end records. Admitted and
ended request IDs must match one-to-one with `released=1`. Cancelled requests
must record at least one suffix hit; sending 64 chunks alone is not proof of it.

HTTP always owns the full-vocabulary class table, so request counters must obey
`numeric_checks = hits + numeric_rejected + short_rejected`. Other query outcomes
also close to `queries`. Whole-span classification means token scans lie between
`32*hits + 3*(numeric_rejected+short_rejected)` and
`64*(hits+numeric_rejected) + 31*short_rejected`. `numeric_ns` remains inside
`query_ns`; never add both. These checks do not replace source-level contracts.

The classification table is 248320 bytes, capped at 262144, with 243 undecodable
padding IDs in the current model. Its construction/release and request index
init/append/query/release costs are reported separately. The bounded request
payload, object and shared table all occupy physical RAM. RSS, NVML and system
cache overlap; do not sum them or infer attributable RAM saving from independent
peaks. Startup auto and naturally inherited page cache are preserved; there is
no manual preloading, drop_caches, extra warmup or SSD-cold claim.

The one-second host sampler and outer NVML monitor cover the full stage. Stale
host sampling beyond five seconds, monitor failure or competition is ENV_ABORT.
Only owned process groups are terminated. Lifecycle's local 90/120-second waits
are failure triggers; its 900-second step and four-hour stage limits are the hard
deadlines. First contract, HTTP, quality, identity, budget or environment failure
stops the candidate without repair/retry in this run. Intermediate performance
results do not alter the fixed remaining request order. Finish with separate
evidence validity, measurement completeness, statistical and engineering fields.

## Preparation and commands

Root freezes the final rule, build, reuse record, contracts and decision card
before invoking preparation. The output stage must not already exist:

```bash
tools/evalscope/.venv/bin/python -B tools/mtp_suffix_admission/prepare.py \
  --stage .q4t-work/suffix-admission-20261024/acceptance-v1 \
  --candidate .q4t-work/suffix-admission-20261024/build/q4t \
  --contracts .q4t-work/suffix-admission-20261024/contracts.json \
  --reuse-record .q4t-work/suffix-admission-20261024/reuse.json \
  --extra-file .q4t-work/suffix-admission-20261024/build-identity.json

python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/suffix-admission-20261024/acceptance-v1/lease -- \
  python3 -B tools/mtp_suffix_admission/run_stage.py \
  --plan .q4t-work/suffix-admission-20261024/acceptance-v1/frozen-plan.json
```

The supplied contract list must be nonempty and total at most 1200 seconds.
Include the ten new stage evidence contracts after the first HTTP gate:

```bash
python3 -B -m unittest discover -s tools/mtp_suffix_admission \
  -p test_stage_audit.py -v
```

Preparation snapshots the actual candidate/parent CMake caches, binary/release,
sources including new tools, fixtures, subset and reuse dependencies. The new
decision card is included automatically; extra build/replay artifacts can be
bound with `--extra-file` or contract `files`. Tokenizer/config/subset source
shard receive content hashes; the other immutable model shards get file metadata
without claiming newly computed full-model content hashes. Output identities
and the completed raw run must be sealed before any deployment decision.
