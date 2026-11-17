# Fixed native SepFormer metadata

Configuration and tensor metadata from the pinned SpeechBrain model; no model
weight or speech sample is distributed here. Full identities and Apache-2.0
attribution are in `third_party/licenses/speechbrain/README.md`. The canonical
deployment config is `tools/native_separator/model-config.json`.

The 417 tensors retain their original F32 shapes and values; conversion prefixes
encoder, masknet and decoder names to avoid collisions. The complete positional
buffers are preserved. These fixtures qualify contracts, not audio business.
