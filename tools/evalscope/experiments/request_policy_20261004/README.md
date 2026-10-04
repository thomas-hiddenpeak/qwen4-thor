# Historical request-policy controller and audit snapshots

[originals/](originals/) preserves the original controllers and offline checkers
used in the bounded phase. [dependencies/](dependencies/) contains the immutable
raw-resource core. The [evidence packet](../../../../docs/evidence/offload-request-policy-20261004/)
provides the [dependency index](../../../../docs/evidence/offload-request-policy-20261004/dependencies.json)
and [final package inventory](../../../../docs/evidence/offload-request-policy-20261004/package-manifest.json).

Do not run or import these archived files. They bind absolute original paths,
a specific tested commit and one-shot outputs; some execute at module import.
This is reviewable source history, not a portable launcher. A new experiment
requires new paths, frozen commands and identities, without replacing old evidence.

[fixtures/](fixtures/) contains only the two newly introduced boundary inputs.
The [input generator](../../prepare_inputs.py) remains an existing tracked tool;
other fixtures, expected outputs, tokenizer/model, runtime binary and full raw
evidence stay external and are explicitly indexed. A seed alone is not a
self-contained tokenizer/environment specification.

Original-path controllers completed the fixed quality, history and six-tier
requests. The first offline protection checker failed on a proc exe visibility
check; its original bytes and failure are preserved alongside the narrowly
repaired v2. That repair did not cause any model test to be rerun. Archive copies
themselves have not been executed or imported as part of packaging.

Both performance screens are NO_GO. Conditional delivery controllers were only
prepared and were not executed; they are deliberately absent from this source
snapshot directory and remain classified in the local evidence index. No next
optimization, default enable, merge or deployment is authorized by this archive.
