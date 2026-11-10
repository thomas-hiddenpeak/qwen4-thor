# Original nested document fixtures

These deterministic OOXML containers and vector/text PDF are original project
test data. The DOC member is the existing original `file_legacy/word.doc`.
Every nested member contains actual bytes; no host-path or URL lookup stands
in for embedded content. The wrappers test source ordering, provenance,
whole-tree budgets and atomic rejection after a later child fails.

Reproduce with `python3 -B tests/fixtures/file_nested/generate.py --output
.q4t-work/nested-reproduced`. No external codec, GPU or model is used to
create these fixtures; extraction tests run the actual isolated decoders.
