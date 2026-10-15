# Completion HTTP 02 independent raw audit

2026-10-09; reviewer `completion_validation_review`. **585/585 checks pass**,
with 421 read-file bindings, including 34 frozen HTTP identities, all 288
build source identities and eight current binaries/cache artifacts. No original
contract/parser module was imported; no model, HTTP or test was run by this
reviewer. The offline audit source and full checks are retained beside this file.

## Evidence and actual result

- Production binary: `eae6949bb275e9c97c3e37c505eddc0988f585daaab01ec3b587c02b80cf5872`.
- Fault link variant: `53279a1ccc1e0c0033ea97b7bacc660244117c02bd23b603cf89b7b158037588`.
- Frozen manifest SHA256: `6b60b580265c6cb46ee58e933f2e2043a35b518ef730e32d0a987ccac90b516e`.
- Exactly nine T4 variant generation requests and one production sequential
  request are present. The raw counters are 9/5/4/0 and 1/1/0/0 for
  total/success/error/abort. No additional generation is hidden by the summary.
  Inputs are the frozen 1K prompt; greedy, seed20260920, max256 and streaming
  usage are unchanged. Effective S1/8192/208896 and real MTP load are logged.
- Raw decoded body bytes match each response JSON and transport byte count;
  HTTP200/SSE content type, request identity and one final DONE are intact.
  Five T4 successes each return 1024 prompt + 256 completion tokens, length
  finish and no error. Every recovery has exactly the fresh control's full
  text, usage, finish and error fields. Its actual path also exactly matches:
  73 T4 steps/verifies/extends, 19 restores, four emitted ordinary tail tokens
  and three ordinary forward calls. The tail is explicitly output_limit.
- Four planned failures all occur during the first real step at committed
  position/history1024. The raw responses contain precisely the expected
  final generation_failed error and DONE; no normal finish/usage or content.
  The failure's path log says plain because no step completed; imported T4
  step/verify observations and zero ordinary calls establish it is not a
  silent ordinary decode fallback.

## Completion and recovery boundaries

Raw event order, independently parsed from the server log, establishes:

1. Sequence-ID upload injection follows the real copy, then the inner verify
   owning thread/stream completion precedes verify return with zero valid
   checkpoint rows/slots. A separate outer step completion precedes step
   return and End.
2. Completed verify, first natural restore and first actual extend attention
   injections each have their own later same-thread/same-stream outer checked
   completion. Natural restore coverage occurred without another prompt.
3. All four failed step returns have count0, next_b=-1, next_d0=-1, unchanged
   caller position/history and no pending work; next_g is explicitly invalid
   and not read. The unchanged built observer asserts actual comparisons,
   not just printed constants, and issues no return-side CUDA repair wait.
4. Every failed sequence is checked in Failed before real End, then in Idle
   with empty history/position0/pending0. Fresh Begin and draft Reset precede
   each subsequent recovery. All after-request health/metrics raw responses
   show a healthy GPU, one free slot, zero active chats and zero body bytes.

These statements rely on the audited link observer and its actual imports;
its logs do not constitute independent dumps of every GPU cache/state byte.

## Shared production path and direct checks

The one production sequential request executes 71 steps, 142 draft forwards,
255 target T1 forwards, zero T4 and 71 extends, plus one unconsumed final tail
output. Full text equals M1's explicit sequential fixture, SHA256
`0e1e7da55f600ba0bf8d5eb598b33eec8fab3dc134b41ff482dab803a5562d15`.
M1 input/output counts are the old client DB columns: its raw usage was absent.
The new SSE directly contains 1024/256/1280; this does not invent historical
raw usage or pretend a new ordinary-mode control was run here.

The same source/build bindings connect the real Thor CUDA18 copy results
(pageable/pinned/device × default/nonblocking ×1/4/8192), guards/predecessor
ordering, five named policy tests, sequential control CTest and 43+11 Python
contracts. All retained execution records are zero-exit, with no warnings.
The original link failure remains in summary-01/build-01; exact Step-symbol
correction is recorded separately and only the affected variants rebuilt.

Both measured servers report graceful shutdown and exit0, without forced
cleanup; their process groups127651 and129732 were independently absent in
/proc at audit time. There were no observed leftover child group members.

## Limits and audit-tool correction

T4 control text SHA256 is
`055d47b9339875bc252f9e2caa2ad5760b5f13fa743973cdb7a28ce32833e92d`,
which differs from sequential. Same-mode recovery does not resolve that
numerical admission gap. This group proves its four logical failure contracts
and a shared-path regression only: it does not qualify broad semantic quality,
full-state equivalence, exceptions, fatal CUDA/OOM recovery, multi-sequence or
nondefault-stream T4, or five-tier performance.

The independent audit's first attempt reported exactly one tooling failure:
it expected the older CTest summary wording rather than the observed CMake
4.4 wording `100% tests passed out of 1`. The raw named test was Passed and its
exit0/log SHA were already correct. The first audit JSON and script are saved
as `completion-http-independent-review-02-first-attempt.json` and
`audit_completion_http_independent_02.first-attempt.py.txt`. Only this text
recognizer was corrected; the final offline read passed585 checks. No model,
HTTP or test was repeated. This audit does not close the overall Goal.
