# Fixed-k3 suffix acceptance

This directory freezes and runs one bounded performance experiment. It does not
change the installed binary or declare an acceptance decision. Generated files
belong in a new `.q4t-work/` directory. Model and reference directories remain
read-only.

The fixed budget is 177 inference HTTP requests, 19 service startups, four hours
including at most 1200 seconds of direct contracts, no warmup and no retries.
Only an accepted candidate may enter a separate deployment stage of at most
three startups and three health requests. HTTP control and health endpoint calls
in the cancellation scenario do not generate tokens and are recorded separately.

Order and scope:

1. Candidate full-head suffix-on quality: the existing 11 exact retrieval cases.
   This is the first test. Source inspection, fixture preparation and compilation
   are allowed before it; no host or GPU test precedes this gate.
2. Frozen direct contracts, then subset suffix-on and plain quality, 11 each.
3. Full suffix-off then on; subset suffix-on then off. Each arm runs the five
   standard contexts with three 256-token responses each and three new scenarios
   with three 256-token responses each. Full and subset results stay separate.
4. Current accepted parent plain and candidate plain, each five contexts by three
   requests. Candidate plain requires the parent prompt and output identities.
5. Full and subset suffix-on cancellation, each control, interrupted decode,
   and fresh recovery on the same slot: three inference requests each.
6. Four separate timing arms, one request for each of the three scenarios. These
   twelve requests enable `Q4T_MTP_TIMING` and cannot serve as throughput samples.

Five-context fixtures are copied from the previous accepted stage, preserving
record-boundary preparation and actual token-count checks. The new repeated
code, repeated prose and nonrepeat prompts are fixed in `prepare.py`; no response
is observed while preparing them. Development materials use artificial token
sequences and must remain separate. The nonrepeat label describes the requested
task, not a promise of zero suffix hits. All observed hits must be reported.

The investment goal is at least 5% median final decode gain on both repeated
scenarios, assessed independently for each head. This is not a tolerated loss
on any protection item. Report every five-context TTFT/decode result, ordinary
decode, nonrepeat cost, quality, actual output lengths, suffix attempts/hits and
acceptance distribution, initialization/query/update/free cost, verify and draft
extend cost, and unified RAM. One service startup per group does not establish
cross-start statistical noninferiority. No generic noise band or loss tolerance
is inferred from these samples. Cross-arm output differences need explicit
review; target verification alone does not prove output equivalence.

The startup configuration remains the current default resource reservation.
Cache state is naturally inherited: no `drop_caches`, no manual cache filling,
no extra warmup and no physical-cold-SSD claim. A one-second sampler records
global `/proc/meminfo` and the driver's descendants. These quantities overlap
and cannot be added to NVML memory as independent physical allocations. Brief
peaks and sub-poll external interference remain outside the sampling guarantee.
A last-sample age above five seconds, a dead sampler, or failure to join it within
five seconds is `ENV_ABORT`; a live but stalled sampler does not count as healthy.

Before preparation, finish compilation and provide two JSON files. The contracts
file is a list of objects with `name`, a command argument array, and `timeout`;
the sum of timeouts must be at most 1200 seconds. Optional `files` lists additional
executables or inputs to hash. Commands may not contain HTTP inference that is
absent from the frozen request budget. A reviewed reuse record identifies prior
evidence paths, hashes, and the specific unchanged dependencies that permit
reuse. In particular, suffix-off quality reuse needs its own dependency audit;
it is not automatically validated merely by listing a prior file.
Use repeatable `--extra-file` arguments to freeze build logs and separate build
identity records. The `cmake/` configuration sources are always included.

```bash
tools/evalscope/.venv/bin/python -B tools/mtp_suffix/prepare.py \
  --stage .q4t-work/mtp-suffix-acceptance-v1 \
  --candidate .q4t-work/mtp-suffix-build/q4t \
  --contracts .q4t-work/mtp-suffix-contracts.json \
  --reuse-record .q4t-work/mtp-suffix-reuse.json

python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/mtp-suffix-acceptance-v1/lease -- \
  python3 -B tools/mtp_suffix/run_stage.py \
  --plan .q4t-work/mtp-suffix-acceptance-v1/frozen-plan.json
```

The stage directory must not already exist. Preparation copies the parent binary,
its actual CMake cache and release metadata; hashes the candidate, artifacts,
source and test inputs; snapshots source including untracked files; and records
model file metadata. The subset source shard, tokenizer/config and artifact files
receive content hashes. The remaining immutable model weights receive size,
mtime, device and inode checks, not a claim of fresh whole-model content hashing.

One outer `gpu_exclusive.py` process holds the shared lease for every test and
the gaps between them. The driver requires that guard ancestor and the locked
repository lease. Every step has an owned process group and a fixed timeout.
Lifecycle's 90/120-second event/future waits trigger failure handling but are not
independent hard exit bounds: thread-pool cleanup can still await the HTTP reader.
The parent driver's 900-second lifecycle-step deadline and four-hour stage
deadline are the hard bounds and stop that owned process group if cleanup stalls.
First failure, timeout, changed identity, monitor failure or environment conflict
stops subsequent steps. Only owned processes are stopped. The GPU guard's
`ENV_ABORT` is separate from candidate failure; a resource-sampler error also
sets the stage state to `ENV_ABORT`, even if the outer guard records the child's
nonzero exit as `COMMAND_FAILED`.

Keep all raw results, including failures and incomplete matrices. A successful
driver ends as `MEASURED_REQUIRES_REVIEW`; it is not a deployment authorization.
Review evidence validity, measurement completeness, statistical conclusion and
engineering decision separately. Budget exhaustion never adds requests or
changes parameters. Deployment and rollback remain a separate reviewed action.
