# Independent BF16 MTP head — rejected research candidate

This stage ended `NO_GO_THIS_ROUND`. The tested candidate failed its first HTTP
quality gate. The two statically identified dimension errors were corrected and
rebuilt, but the repaired code has **not** passed runtime validation. Do not
deploy it or interpret the build as acceptance. The full head remains default.

See [the stage report](../../docs/MTP_SUBSET_HEAD_2026-10-23.md) and its frozen
identity, request budget, failure evidence and explicit unexecuted checks.

`convert.py` selects exactly 65536 original token IDs using the frozen 24-record
calibration input/response corpus. It copies BF16 source rows without conversion,
forces tokenizer/generation special tokens, sorts rows by original ID, and emits
`head.bf16`, `token_ids.bin` (little-endian int32), and `manifest.json` under
`build/` or `.q4t-work/`. No GPU or model generation is used during conversion.
Zero-frequency ties are filled by ascending original ID; calibration support is
only 629 distinct tokens, so this is a narrow initial shortlist, not a trained
proposal model or a demonstrated general-purpose coverage guarantee.

The explicit experimental option is `--mtp-subset-head DIR` on serve/generate.
Serve rejects this option with `--no-mtp` or non-k3. The artifact loader checks
fixed dimensions, dtype, hashes of payload/source/tokenizer/generation files,
sorted unique in-range IDs and required special tokens. Failed explicit loads
fail startup. Source/config files must stay immutable for the load; these hashes
are consistency checks, not an adversarial artifact authenticity system.

The owned head and device mapping add 335806464 bytes. Original full-head/shared
logits allocations remain; subset logits are packed by `MtpModel::DraftVocab()`.
The budget includes host staging and a conservative 4 GiB allowance for the files
read by validation. Runtime file sizes must fit this allowance. This is not a
physical RAM cap: measure unified RAM and file-cache residency independently.

All five production draft selection sites call `DraftArgmax`; the two target
verification argmax sites retain the full domain. Embedding retains the full
domain. BF16 arithmetic is retained, but shape-dependent dispatch/rounding and
changed proposals can affect final outputs. No lossless-output claim is made.

`contract.cpp` prepares projection, repeated-call, ID-map, tie and release checks.
`replay.cpp` adapts the archived k3 replay to current APIs and local output rows;
it has independent target/checkpoint comparisons and a forced reject case.
The optional fourth argument of `q4t_mtp_lifecycle` enables this artifact for its
real MTP loading/failure tests. These tools built, but were **NOT_RUN** because
HTTP failed. They do not establish full termination/acceptance-state coverage.

`run_stage.py` is the frozen historical driver, not an auto-resume instruction.
It requires its saved plan and original directory layout, rejects changed
binary/tool identities and existing output directories, and must only run under
`tools/evalscope/gpu_exclusive.py`. The original plan hashes the failed binary;
the repaired binary intentionally no longer matches. This round is closed;
no new GPU requests are authorized by this README.
