# SpeechBrain ECAPA-TDNN sources

Native speaker embedding follows SpeechBrain's ECAPA-TDNN graph and Fbank
preprocessing. Source commit `c0241278f93ef02cd5191f03caf4e5f8e1292bf6`:
https://github.com/speechbrain/speechbrain/tree/c0241278f93ef02cd5191f03caf4e5f8e1292bf6

Source authors include Hwidong Na (ECAPA-TDNN), Mirco Ravanelli,
Peter Plantinga, Sarthak Yadav and Sylvain de Langen (feature processing).
Apache-2.0 license is retained in LICENSE. Model card declares Apache-2.0:
https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb/tree/0f99f2d0ebe89ac095bcc5903c4dd8f72b367286

Original `embedding_model.ckpt`: 83,316,686 bytes, SHA256
`0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2`.
Weights remain optional deployment assets. The offline converter preserves
all tensor values and records the converted identity. Production does not
load pickle or require SpeechBrain/PyTorch services. Classifier labels for
training identities are not part of the embedding backend.

Feature/layout contracts and native computation are tracked separately in
`docs/SERVE_NATIVE_DIARIZATION_2026-11-18.md`; source/asset preparation alone
does not qualify diarization API behavior or general model accuracy.

## SepFormer overlap separation

The native separation work follows SpeechBrain's dual-path and Transformer
graphs at `fc2eabb7416b0ae68b3baaadb39972ea1d153985`. Authors include Cem
Subakan, Mirco Ravanelli, Samuele Cornell, Mirko Bronzi and Jianyuan Zhong;
the existing Apache-2.0 LICENSE is byte-identical to this revision's license.

Model: https://huggingface.co/speechbrain/sepformer-wsj02mix/tree/3a2826343a10e2d2e8a75f79aeab5ff3a2473531

The model card declares Apache-2.0. Original encoder/masknet/decoder digests
are pinned in `tools/native_separator/convert_weights.py`; the offline converter
preserves all F32 values and position buffers with explicit component prefixes.
Production does not load pickle or require PyTorch/SpeechBrain services.
Weights and upstream speech recordings are not distributed in this repository.
The model's published two-source WSJ0-2Mix results do not establish arbitrary
domain accuracy or successful q4t overlapping-transcription business.
Implementation and validation: `docs/SERVE_NATIVE_OVERLAP_2026-11-18.md`.
