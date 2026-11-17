# Fixed ECAPA metadata fixtures

`config.json` records the native execution contract derived from the exact
model hyperparameters and SpeechBrain defaults. `tensor-header.json` is the
full 231-tensor metadata from the bit-preserving offline conversion; it is
not a synthetic shape map. Original and converted hashes and Apache-2.0
source/license are recorded in `third_party/licenses/speechbrain/README.md`.
Actual checkpoint tests are opt-in; no model weights are stored here.
