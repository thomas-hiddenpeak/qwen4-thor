# T4 completion candidate independent review

2026-10-09. Reviewer: mainline_integration_audit.
Static review only: candidate not applied, built, tested, or exercised with a
model. Only this review artifact was written.

## Decision and identity

**No blocking omission found within the proposed S1 / slot 0 / k=3 /
default-stream, ordinary Status-return boundary.** The candidate is suitable
for the next frozen implementation and validation group. This is not T4
admission, mathematical equivalence, failure recovery from a sticky CUDA
fault, or a performance pass.

Reviewed live source HEAD:
`90d5f539a77b6ab2d6c5bc5fbaa2cadfced778d1`.

Reviewed candidate identities:

- `candidate.patch`: `de20278bc264f65f8122a1359882e35d2d3e41f1af865c7d762e4efb0b79a3ce`
- `REVIEW_AND_PROTOCOL.md`: `4c9230e88a1909e1a627ae07d6816886724aca9b0d9694f57675c82496bfc260`
- `manifest.json`: `c0a3d802f4ab7de5a87676bfe5dd1ff575be7617068bdb7ba26f80cde57c1e17`

All five existing-file base hashes and all six candidate hashes match the
manifest. The new helper does not exist in live source at review time.
Parent-owned untracked `docs/MTP_COMPLETION_FIX_2026-10-09.md` was not edited.
Candidate line references below are relative to
`t4-completion-candidate/files/`; unchanged-file references are live source.

## Completion and host ownership

1. **Fast step:** candidate `src/mtp/mtp.cu:1248–1260` checks stream completion
   before returning from every Status branch after the first submission at
   line 1278. I followed draft seeding, each draft, verify upload/forward,
   argmax readback, natural restore, gather allocation/upload/launch, extend
   upload/forward, next seed/trunk copy, and final success. Their returns all
   reach `cleanup`. Function-owned `seq_of`, `h_pos`, verify packing,
   `gather_off`, `ext_seq`, `ext_ids`, and `ext_pos` remain alive until
   cleanup completes. The caller retains `d0` for the call. Checked completion
   can replace an otherwise successful Status with failure.

2. **Draft position overwrite is currently ordered:** candidate
   `mtp.cu:1313–1332` reuses `h_pos` between iterations. On the actual full
   forward path with B positive, success necessarily passes
   `MtpForward:1729` → live `src/mtp/moe_bf16.cu:479` →
   `MoeBf16RoutedForward:346`, whose `cudaStreamSynchronize(stream)` must
   succeed. That synchronization follows the position upload on the same
   stream. A failure exits through outer cleanup before another overwrite.
   This is an established code dependency, not an assumption about pageable
   copies or attention readback. Add a nearby comment or preserve this
   invariant if the MoE wait is later removed; a future asynchronous MoE
   implementation should use immutable per-iteration position slices.

3. **Verify-local ownership is repaired at the correct level:** candidate
   `src/model/model.cu:1047–1090` constructs positions, sequence IDs, RoPE
   values, int64 IDs and history before submission. RoPE upload sources at
   lines 1119–1121 refer to stable vector elements. All six immediate
   upload/embedding/expand error exits and the final `RunLayers` result use
   `finish` at lines 1094–1103 / 1141. Thus an outer-step wait is not being
   used to save already-destroyed verify vectors. Checkpoint metadata is
   invalid on verify failure and is published only after successful
   completion at lines 1142–1144. `RunLayers` itself owns no transient host
   upload vector; PLE's upload staging is object-owned. Auxiliary expert
   work retains the existing join/fallback boundary in live
   `src/quant/moe_gemm.cu:528–550`.

4. **Shared forward callers:** strict draft and extend position arrays remain
   unchanged in live `src/mtp/mtp_sequential.cpp:181,238` until their
   `FinishSequential` boundaries. In S1 initialization, candidate
   `mtp.cu:772–779,788` uploads caller-owned prompt slices into `d_pos`;
   the forward then reads that device source on the same stream.
   Reusing `d_pos` in the next chunk preserves stream order, including
   unused-tail skip. Nonchunk initialization passes its allocated/persistent
   device positions at lines 874 / 886. The direction helper therefore
   addresses both actual device-source initialization and host-source strict
   calls; it does not establish completion by itself.

## Failure outputs and sequence state

Candidate fast-step lines 1208–1216 establish zero / -1 / -1 sentinels after
output extent validation; cleanup reinstates them on any later failure.
`accepted_tokens` and device `next_g` may be partially written, are explicitly
invalid on failure, and are not consumed by the scheduler. This avoids an
unneeded clearing kernel and does not promise rollback.

The fast core does not advance caller position/history. Candidate
`src/server/chat_scheduler.cpp:392–395` marks affected sequences failed under
model ownership before `done` / notification publication at lines 401–418.
The request handler rejects a nonpositive count before token consumption
(live `src/server/chat_generation.cpp:845–849`), ends the failed request,
and does not decode from its partially mutated state. A subsequent fresh
request resets model state through the existing begin path. A successful
verify followed by a later step failure may still leave checkpoint metadata
describing that verify until reset; the contract is fail/end, not reusable
state or invalidation of every successful intermediate checkpoint.

## Limits and follow-up notes

- **Exceptions remain excluded as documented.** The fast step still allocates
  vectors after GPU submission (for example candidate `mtp.cu:1296,1313,1445`).
  Its lambda is not an unwind guard. `bad_alloc`, tracing exceptions, or
  exception-based recovery cannot be described as completed/contained by this
  patch. This is not an additional model test requirement for the frozen
  Status-error protocol.

- **Do not broaden the initialization ownership claim to multi-sequence
  chunking.** Candidate `mtp.cu:781–785` retains a preexisting block-local
  `seqid` whose async H2D source expires before the forward. That path is
  absent with max_seq=1. Before qualifying it, keep the source until checked
  completion or use a function-owned immutable buffer. General initialization
  error handling and nondefault-stream synchronous-copy ordering also remain
  outside this group.

- **Do not claim arbitrary nested host readback fault coverage.** The existing
  draft MoE's local `counts_h` readback error branch
  (`src/mtp/moe_bf16.cu:340–349`) has no independent drain on a reported copy
  error. The four proposed logical injections do not intercept that site.
  The new boundaries demonstrably protect their owned upload sources; they
  are not proof of every possible inner function's host-output ownership
  under synthetic post-success errors or sticky device faults.

- **Diagnostic timing ends before the new final completion.** Candidate
  `mtp.cu:1549–1568` sets engine results / engine_end / phase timing before
  line 1570 drains. Request E2E includes the wait, but these engine intervals
  can omit it and must not be used as complete cost evidence. Prefer final
  checked cleanup before recording successful engine_end if those diagnostics
  are to be used in the next performance phase. The current protocol makes no
  such performance claim.

The two new stream waits intentionally strengthen API completion semantics.
They may add host blocking / launch gaps. Kernel arithmetic, precision, token
packing values and acceptance calculations are not changed by the patch;
correcting the copy direction is nevertheless a shared runtime change and
requires the proposed direct and strict integration checks.

## Bounded validation handoff

The frozen proposal of 18 real-CUDA helper cases, nine T4 variant requests,
and one normal-candidate strict request addresses this patch's stated scope.
Retain the fixed input, first-natural-restore rule, exact recovery comparison,
real-call-before-logical-error wrappers, and post-injection synchronization
ordering assertions. A pre-injection wait must not satisfy the outer drain
check. Observe failed stage before end and reset before the next request.
Preserve all four expected error responses.

No extra model request is required by this static review. Do not count any
planned case as passed, relabel old HTTP evidence as this binary, infer
five-tier performance preservation, or expand the result to stop/tail
handling, nondefault streams, multi-sequence initialization, arbitrary
exceptions, or full T4 equivalence.

