# Numeric suffix admission: offline index diagnosis

No GPU, HTTP or new candidate execution. The CPU helper links the existing numeric-stage tokenizer libraries and compiles the unchanged frozen suffix source. The helper build log has no warnings.

116 sealed requests / 196 source files / 8 unique prompt and output sequences. Every selected SQLite, output text and server log matches the original raw seal. Each request has 256 individual SSE token chunks; every chunk has exactly one matching production Decode(skip_special=true) ID across model IDs [0,248320). Tokenizer vocabulary is 248077 and all 243 padded IDs fail Decode. Prompt Encode counts match the server.

Client timing bursts are inferred using the largest adjacent positive-ITL ratio, then split at the independently logged MTP committed count. All 116 request acceptance histograms close without changing the split rule. This is not native server step tracing.

58 ON requests reproduce every logged suffix hit and its acceptance histogram. 52 close every logged matching/admission counter. Six subset repeat requests contain 14 matched zero-accept steps: their native d0 is not observable, so the output records UNKNOWN_D0 and leaves a corresponding unassigned aggregate d0-mismatch remainder. No individual d0 is invented. The four diagnostic probes have no such unknown step.

## One fixed candidate: minimum admitted exact match 32

| Fixture | Full known old hits retained | Subset known old hits retained | All-prefix maximum match |
|---|---:|---:|---:|
| context-1024 | 0/8 | 0/10 | 13 |
| context-8192 | 0/7 | 0/7 | 13 |
| context-45056 | 0/14 | 0/15 | 12 |
| old-8k | 0/7 | 0/7 | 21 |
| repeat-code | 55/62 | 55/62 | 64 |
| repeat-text | 55/62 | 55/62 | 64 |
| old-repeat-code | 55/62 | 55/62 | 64 |
| old-repeat-text | 54/61 | 54/61 | 64 |

The two blocks agree. Retained known matches in new and old repeat text/code are all a3 except one a2 in subset new repeat-code. These are old-path classifications; later step boundaries, neural state, acceptance and actual throughput under the new gate require new evidence.

## Why remaining 45K matches are weak continuation evidence

Full has 14 hits, histogram [0,3,5,6], lengths 3..10; subset has 15, [0,3,5,7], lengths 3..11. Eight partial hits per head copy phrases from source records into an enumerated explanatory answer. The answer paraphrases the continuation or changes punctuation/list boundaries. Except the irrigation example, their equally longest indexed candidates have one continuation; a continuation-consensus gate would therefore retain these mistakes.

| Position (full) | Exact match | Historical proposal | Actual continuation | Accepted |
|---:|---|---|---|---:|
| 32 | ` traceability, measurements, custody, maintenance, and` | ` review decisions.` | ` review decisions based` | 2 |
| 92 | ` transcript identifies uncertain words without silently replacing them` | `.\nEntry` | `.\n2` | 2 |
| 107 | ` original survey points beside corrected` | ` coordinates. Review` | ` coordinates; reviewers` | 1 |
| 113 | ` compare both versions` | ` before approving a` | ` before approving revised` | 2 |
| 132 | ` humidity readings from` | ` separate probes.` | ` separate probes;` | 2 |
| 143 | ` irrigation schedule changes` | `.\n\nTask` | `.\n4` | 1 |
| 157 | ` each repaired binding` | ` before returning the` | ` before returning book` | 2 |
| 195 | ` recorded changes persisted.` | `\nEntry ` | `\n6.` | 1 |

These partial accepts alone do not prove extra target work relative to OFF; comparison at the same pending position is required. The separate runtime audit performs that alignment.

## Old 8K

Both heads retain seven numeric-gated hits with [0,0,2,5]. At p27, a seven-token match proposes ` correctness.\n` but actual next three tokens end in ` correctness. Let`; it accepts two. At p78/P8237, the 19-token source span starts ` cache can reuse` and ends `measurement of physical`; the proposal ` memory traffic.` differs from the quoted answer ` memory traffic."` and accepts two. Both have one equally-long continuation, so consensus does not solve the quotation boundary. Runtime native traces independently establish that p27 also accepts two with suffix OFF, while p78 accepts three OFF and two ON and shifts the remaining output positions by one. Thus the latter explains the additional ordinary-tail forward.

The sole >=32 gate rejects all known four-probe hits, including the length-19 quotation boundary, while retaining 54 or 55 known long-copy hits per repetition request. It avoids a semantic byte-pattern taxonomy and needs no candidate rescan, dynamic k or time-based selection. This is a bounded engineering hypothesis, not an acceptance result.

Full per-step source positions, global candidate IDs, exact spans, actual continuations, bounded competitors and counter closure are in token-burst-replay.json. Frozen source, tokenizer, linked libraries, helper and input hashes are recorded there. The original numeric-stage decision and raw evidence remain unchanged.
