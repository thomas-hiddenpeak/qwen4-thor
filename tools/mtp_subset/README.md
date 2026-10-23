# Independent BF16 MTP head — bounded opt-in candidate

The repaired 65536-row BF16 candidate passed the separately authorized resume:
22/22 quality requests, all direct contracts, and both-mode five-tier comparisons
(82 HTTP requests total). MTP decode medians improved 7.56%–12.68%; same-mode
parent/candidate outputs matched on this corpus. This is bounded opt-in engineering
acceptance, not a statistical noninferiority or general lossless-output claim.
The full head remains the default. See [the resume decision](../../docs/MTP_SUBSET_RESUME_2026-10-23.md).
The original failed round remains separately preserved in
[its report](../../docs/MTP_SUBSET_HEAD_2026-10-23.md).

The subset head and the subsequent SHA2 startup optimization are now merged into
main. The deployed binary remains `f356b2c9`; see the
[integration and rollback record](../../docs/MTP_SUBSET_MAIN_MERGE_2026-10-23.md).
The root [startup guide](../../README.md#使用) includes the serve command, the
retained artifact path, option defaults and mode switching. Artifacts are local
under `.q4t-work/` or `build/`, not distributed with a Git clone. The directory
passed to `--mtp-subset-head` must contain all three files described below.

`convert.py` selects exactly 65536 original token IDs using the frozen 24-record
calibration input/response corpus. It copies BF16 source rows without conversion,
forces tokenizer/generation special tokens, sorts rows by original ID, and emits
`head.bf16`, `token_ids.bin` (little-endian int32), and `manifest.json` under
`build/` or `.q4t-work/`. No GPU or model generation is used during conversion.
Zero-frequency ties are filled by ascending original ID; calibration support is
only 629 distinct tokens, so this is a narrow initial shortlist, not a trained
proposal model or a demonstrated general-purpose coverage guarantee.

The explicit opt-in option is `--mtp-subset-head DIR` on serve/generate.
Both require k=3 for a subset. Serve rejects it with `--no-mtp`; generate has no
`--no-mtp` option. The HTTP acceptance does not cover CLI generate. The loader checks
fixed dimensions, dtype, hashes of payload/source/tokenizer/generation files,
sorted unique in-range IDs and required special tokens. Failed explicit loads
fail startup. Source/config files must stay immutable for the load; these hashes
are consistency checks, not an adversarial artifact authenticity system.
ARM SHA2 acceleration is selected automatically by CPU capability, with software
fallback and the same complete hashes; it needs no CLI switch. See the
[startup optimization](../../docs/SHA2_STARTUP_2026-10-23.md).

The owned head and device mapping add 335806464 bytes. Original full-head/shared
logits allocations remain; subset logits are packed by `MtpModel::DraftVocab()`.
The budget includes host staging and a conservative 4 GiB allowance for the files
read by validation. Runtime file sizes must fit this allowance. This is not a
physical RAM cap: measure unified RAM and file-cache residency independently.

All five production draft selection sites call `DraftArgmax`; the two target
verification argmax sites retain the full domain. Embedding retains the full
domain. BF16 arithmetic is retained, but shape-dependent dispatch/rounding and
changed proposals can affect final outputs. No lossless-output claim is made.

`contract.cpp`, `replay.cpp`, the lifecycle harness and the four selected state
tests all passed in the resumed stage. Coverage and limits are in the report:
FP64 projection bounds, repeated calls, mapping/ties, nine invalid artifacts,
allocation failures, a=0/1/2/3, batched termination and independent fresh-draft
reconstruction. Controlled tests do not establish arbitrary-input quality.

`run_stage.py` requires a frozen plan and a new output directory under the source
worktree. Run only through `gpu_exclusive.py`. Neither historical plan authorizes
a new measurement. Original files are retained in source-snapshot-v2; this README
was updated after the frozen run. Analysis/audit and delivery tools take `--base`
pointing to the retained stage directory; they preserve that directory layout.
The acceptance audit is a pre-deployment audit and expects the old default pair.
`deploy_resume.py` requires the matching deployment lease and sealed accepted
package; `--rollback` restores only known old/new identities under that lease.
That tool covers the historical `75e85bac` to `5a456351` pair. For the current
`f356b2c9` deployment, first use the SHA2 rollback tool to restore `75e85bac`,
then use this tool if a full pre-subset rollback is needed. Use fresh lease/output
directories and the commands in the integration record; preserve sealed evidence.
To disable only the subset at runtime, remove `--mtp-subset-head` and restart.
