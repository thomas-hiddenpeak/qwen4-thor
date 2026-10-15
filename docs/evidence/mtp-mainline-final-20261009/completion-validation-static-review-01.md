# Completion/terminal validation static review

Reviewer: completion_validation_review, 2026-10-09. No build, test, or model
request was executed by this reviewer. Parent started its separately frozen
first build while final static review completed. Source identities are in
`completion-validation-static-review-01.json`.

## Decision

No blocking issue found for the frozen S1/slot0/default-stream completion and
terminal validation group. This is readiness to execute the frozen checks,
not their success, broad T4 numerical admission, or performance acceptance.

## Test and harness review

- The real-CUDA copy test executes the production helper over pageable,
  pinned and device sources, default/nonblocking streams and 1/4/8192 rows.
  Guard and integer expectations are independent of the implementation;
  device source initialization is ordered on the same stream. Every resource
  close drains before host vectors are destroyed. CUDA/UVA absence fails.
- Fault wrappers call real operations outside the observer mutex. Step,
  verify, restore and attention imports cross translation units; no reliance
  on same-TU MtpForward interception. The final stop-span symbol demangles to
  the current signature. Final linker imports still require build evidence.
- Injection follows successful real submission/forward. The observer does
  not issue a repair wait when Step returns. Its wrapped completion records
  require the injecting owner thread and stream. The verify-upload case
  additionally requires inner completion and invalid checkpoints; all faults
  require invalid result sentinels, unchanged caller cursor/history, zero
  ordinary fallback, Failed before End and Idle afterward. Exact fresh vs
  four recoveries is enforced both in HTTP fields and actual decode paths.
- Fixed nine+one requests, input and existing response contracts are retained.
  The driver now creates a private process group, records timeout/forced/
  residual cleanup and retains failed in-progress request records. Cleanup
  exceptions cannot silently bypass the group summary. Prelaunch archive or
  import failure also gets a failed summary after evidence-directory creation.
- Archived imports are verified against frozen-sources. The transport ROOT
  is explicitly rebound to manifest root; only its path-explicit request,
  snapshot and read_log APIs are used. New raw SSE usage is preserved; old
  M1 raw-usage absence is explicitly distinguished from DB count evidence.
- Parser now rejects malformed/unknown observer events. Negative cases cover
  inner/outer and stream/thread ordering, stale outputs, checkpoint validity,
  test-side repair, Failed state, unexpected ordinary calls, absent restore
  and recovery mismatch. Their execution is left to the parent's fixed run.
- Copy is a regular CUDA CTest target (not host-only CI), fault server remains
  EXCLUDE_FROM_ALL and has no effect on production link options.

## Independent terminal production review

- Both initialization and per-step admission call the same overflow-safe
  generation policy. The old sequential name delegates to identical logic.
  For k=3, output/context 1..4 cannot run a full step; 5 can. The final pending
  output remains unconsumed, including max1's existing prefill-only path.
- Acceptance checks each reachable target stop before draft equality. If a
  stop occurs in row i, exactly bonus plus i preceding drafts are consumed;
  the existing checkpoint i restores that prefix when i<3. Full acceptance
  uses final recurrent state. Stop is returned as pending correction, with
  next_d0 still -1 from initialization, and no output-seed write.
- Both extend packing and per-sequence readback skip terminal rows while
  preserving original sequence IDs and a compact continuing-row cursor.
  All-terminal T_ext=0 bypasses allocation, back(), CUDA launches and extend.
  This mixed-B code audit is not runtime multi-sequence qualification.
- Generation checks count bounds and pending correction capacity before any
  prefix pointer range or emission; it validates all accepted IDs as non-stop,
  the first token as the pending bonus, correction ID and terminal seed state.
  Sequential result/counter agreement is additionally retained. Failed calls
  cannot enter ordinary tail. Ordinary tails require scheduler B1 arithmetic.
- Terminal trace is marked invalid for the old complete-step timing schema,
  retaining real zero-extend behavior. No numerical/performance claim follows.
- Existing draft h_pos reuse is ordered by a successful full MtpForward's
  mandatory checked MoE counts sync; error paths use new outer completion.
  This matches the other independent completion review. Removing that wait
  later requires immutable upload sources or a replacement boundary.

Exceptions, fatal CUDA recovery, arbitrary nested readback failures,
nondefault-stream T4, multi-sequence initialization and full-state/numerical
qualification remain outside this evidence. New completion costs need the
parent's frozen production five-tier group. Nothing here closes the Goal.

## Build-followup clarification: raw linker spelling

The first formal parent build subsequently failed to link the new Step wrapper.
The guessed suffix `St4spanIKiLm18446744073709551615EE` demangled to the
same C++ signature but did not equal the compiler's actual raw symbol, whose
suffix is `St4spanISD_Lm18446744073709551615EE`. My earlier demangle check
established signature meaning only; it did **not** establish a linkable raw
symbol. The original first-build failure remains authoritative and preserved.

After the parent's wrapper/CMake-only correction, this reviewer independently
read `nm -A --defined-only` and `nm -A --undefined-only` from the built
`libq4t_model.a` and `libq4t_server.a`, plus the real libcudart dynamic exports.
All ten completion and nine terminal wrapper aliases have an exact definition
or runtime export and at least one actual production object import. Both
CMake target wrap sets exactly equal their corresponding source aliases.
`wrapper-symbol-independent-review-01.json` binds those exact strings,
object locations and archive hashes; `wrapper-symbol-defined-01.txt` retains
the selected raw definitions. No additional mismatch was found. This remains
read-only symbol evidence, not link execution or HTTP/runtime coverage by
this reviewer. No source was modified by this followup.
