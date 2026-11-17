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
