# Resident-main budget integration — 2026-10-07

Worktree: `.q4t-work/main-wrapup-20261007/source`, based on main `8ea85b1`.
Selected source: `cb379bd`. This note describes implementation and test intent,
not validation results. The budget agent did not build, run tests, start HTTP,
commit, or change STATUS/log. Root owns the grouped validation and records.

## Included and excluded

Included: explicit feasibility/reason and requested/effective capacities; startup
rejection before ModelOwner::Load when no supported capacity fits; removal of the
zero-capacity-to-original-request fallback; monotone automatic-length search;
explicit context-ceiling capping; MTP-conditional workspace/state/transient
charges; actual PLE head count, buffer dimensions and page-pool charge; KV-page
rounding; main/MTP workspace sizes supplied by runtime sizing functions; integer
saturation and invalid-request/shape rejection.

Excluded: residency/offload configuration and weight subtraction, MoEResidency,
extra residency fixed costs, hot lists, cache strategies, GEMM changes. The full
main index weight estimate is retained. No model/reference content was modified.

Owned changes, including the explicitly authorized review follow-up, are:

- `include/q4t/runtime/memory_budget.h`
- `src/runtime/memory_budget.cpp`
- `src/server/chat_server.cpp`
- `tests/runtime_memory_budget_test.cpp`
- `tests/budget_host/CMakeLists.txt`
- `src/server/server_options.cpp` (review follow-up)
- `tests/server_options_test.cpp` (review follow-up)

## Allocation reconciliation against main

`src/model/model.cu::LoadModel` allocates five per-prefill int arrays, BF16
embedding, two BF16 trunks, and a BF16 PLE embedding when layer 1 is present.
Sequence IDs and the per-sequence part of ragged offsets are separate charges;
the terminal ragged offset is one fixed int. At prefill 8192 the forward buffers
are 419,594,244 bytes for the current 2560 hidden / four-stream model.

PLE heads are `(ple_ngram_size - 1) * ple_heads_per_ngram = 16`, and the output
embedding is `16 * 160 = 2560` BF16 values per token. `PleEmbedding::Create`
allocates pinned FP8 staging, GPU FP8 staging and pinned int64 row IDs for
8192 * 16 rows. `PlePageReader` adds its 32 MiB pool and documented ring estimate.
The modeled PLE working total is 76,571,648 bytes. Dynamic page-piece/group
vectors remain inside the uncalibrated margin.

`LoadDecoderLayer` allocates full-attention KV for `ceil(max_len/16)*16` tokens,
but page tables and both indexer buffers for the exact max_len. The model has
12 full layers, 36 linear SSM/conv pools, one PLE conv pool, and main three-axis
RoPE per sequence. Scheduler/prefill output rows plus scheduler argmax and the
main sequence metadata are charged. At max_len 208896 / max_seq 1, the modeled
state pool is 6,546,454,540 bytes.

`LoadMtp` separately pools its full-attention and three-axis RoPE state. These,
its workspace, full-prompt trunk and draft-extend chunk logits are charged only
when the capability is enabled. The default is plain text greedy / MTP disabled.
The main index is kept as the existing weight estimate. MTP loads independent
BF16 weights from its own index; these are not in the main-index charge. The
full-prompt trunk may be allocated before model_mu_, so multiple requests can
overlap. PerRequestBytes is only a one-request reference subtotal, not the MTP
peak. The header and MTP-enabled startup report explicitly state that independent
draft weights, concurrent overlap and lazy scratch/checkpoints are excluded.
A feasible has_mtp=true result does not establish a complete MTP budget.

Serve supplies the maximum of `ModelHeadWorkspaceBytes` and each layer's
`DecoderLayerWorkspaceBytes`, matching LoadModel's dimensions/topology. MTP uses
`MtpWorkspaceBytes`. Small-T attention scratch can depend on context length;
the pre-cap requested upper bound is used, so a reduced capacity can retain a
conservative workspace estimate. Host-only callers still have a documented
scaled historical fallback, not an exact allocation oracle.

## Existing baseline semantics and corrected boundaries

The accepted text baseline remains explicit max_seq=1, max_prefill=8192,
max_len=208896, MTP/media off. No forward computation, precision or scheduler
code changed. Actual default ModelConfig max_prefill=2048 is preserved when CLI
max_prefill=0; the estimator now uses that effective value instead of the old
8192 assumption. Capacity logging reports both raw CLI and effective values.

The budget API's fraction 0 means default 0.90, as before; it never means bypass.
Server CLI validation already requires a finite fraction in (0,1]. Only the
existing explicit `--no-budget` flag bypasses evaluation and retains its old
ModelConfig/CLI capacity behavior.

Explicit length requests cap at 262144, preserve requested length if at least
one sequence fits, and reduce sequence count first. If even one does not fit,
length may shrink down to 2048; explicit lengths below 2048 must fit in full.
No supported solution returns feasible=false and both capacities zero. Serve
logs the reason and returns failure before model loading; zero never restores
the original request. Automatic length preserves the requested sequence count
(API zero still defaults to eight) and searches complete 1024-token units.
If even 1024 tokens do not fit, it rejects rather than manufacturing a minimum.
Exact fits pass; one-byte deficits reduce only where the contract permits.
Overflowed estimates saturate and fail closed instead of wrapping small.

## Validation commands for root (not executed by this agent)

From the integration source directory:

```bash
cmake -S tests/budget_host -B ../budget-host -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_COMPILER=g++-14
cmake --build ../budget-host --parallel
ctest --test-dir ../budget-host --output-on-failure
```

This independent pure-host target compiles with -Wall -Wextra -Werror and uses
the strict project test runner. Fourteen tests cover infeasible capacity,
explicit/automatic minima, exact-fit/one-byte boundaries, sequence reduction,
requested/effective reporting, context ceiling, default fraction and effective
prefill, independently enumerated resident baseline allocations, MTP off/on,
PLE heads/pool, supplied workspace sizes, KV page rounding, malformed requests,
and overflow. No weights/CUDA required by this target.

Runtime integration should additionally record an intentionally infeasible
small mem-fraction startup: budget feasible=0, nonzero exit, no model_load phase
and no listener; compare the real baseline's requested/effective capacities and
actual workspace fields. Root owns the full Thor build and applicable grouped
HTTP quality/performance checks under EVALUATION.md. Neither host-only formula
tests nor startup refusal constitutes model quality/performance acceptance.

## Known coverage limits

This remains an allocation estimate, not a physical RAM cap or OOM guarantee.
File cache, other processes, driver/allocator retention, loading overlap and
all host dynamic structures are not measured here. The two-billion-byte margin
is retained without recalibration. Router tracing/vision transient peaks and
complete MTP speculative lazy scratch/checkpoint peaks are not independently
modeled or newly accepted by this change. MTP/media generation, full memory
profiling and alternate architectures are outside this resident-main repair.
A feasible result only proves that the selected modeled terms fit the estimate.

## Review follow-up: prefill input bound

Cross-review found a blocking route before ComputeMemoryBudget: the previously
valid `--max-prefill 214748365` can overflow `M * topk` in workspace sizing.
The real FullAttentionForward already rejects T > 8192. ValidateServerOptions
now enforces max_prefill <= 8192, while 0 keeps the existing ModelConfig default.
ParseServerOptions invokes this common validator before publishing parsed
options; ChatServer::Start invokes it before tokenizer/model load and all
workspace sizing. The existing --no-budget bypass does not bypass this execution
bound. The literal is documented against FullAttentionForward; no CUDA header
was added to the pure-host options code and no model-wide refactor was made.

One new existing-host contract tests 0/8192 acceptance, 8193/214748365/INT_MAX
rejection, transactional parse failure, direct programmatic validation, and
rejection even with no_budget=true. The tests were added but not executed by
this agent. Root must include the normal host target in grouped validation.
Only a complete root validation can mark this remediation verified.

The MTP-off acceptance scope is unchanged. Independent MTP weights/concurrent
trunks were confirmed during review and are now expressly excluded from the
MTP estimate/report rather than described as one serialized peak. The MTP
conditional-allocation test also checks that the subset limitation is emitted
only for has_mtp=true.
