# Qwen3-ASR source and metadata attribution

Qwen3-ASR: Copyright 2026 The Qwen team, Alibaba Group and the HuggingFace Inc.
team. All rights reserved. Licensed under the [Apache License 2.0](LICENSE).

- Official code reference: [QwenLM/Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR/tree/7c6daf77a2421100f5fb066495372c00129d39ff).
- Model metadata: [Qwen/Qwen3-ASR-0.6B](https://huggingface.co/Qwen/Qwen3-ASR-0.6B/tree/5eb144179a02acc5e5ba31e748d22b0cf3e303b0).
- Current use: public configuration/tensor-header fixtures, ASR added-token
  metadata, and independently written C++ architecture/weight validation,
  tokenizer loading and log-mel feature computation. No model weights or
  Python inference implementation are distributed in this source tree. The
  original configuration bytes are retained; the safetensors header is
  reserialized as fixture JSON.
- Runtime integration remains in progress. Neither this metadata nor the
  validator alone constitutes a working transcription endpoint.

The original license is included unchanged. The fixed official code was
reviewed for the computation contract; any later source adaptation must retain
its copyright/license notice and describe modifications here and in README.

Tokenizer/feature CPU reference: Transformers 5.16.1, tokenizers 0.23.2;
the exact installed source hashes accompany the fixtures. The reference
`transformers.audio_utils` is Copyright 2023 The HuggingFace Inc. team and the
librosa & torchaudio authors, Apache-2.0. The C++ feature path implements
standard DFT/Hann/Slaney equations independently; the reference runs only to
generate synthetic validation data. No Python, NumPy or Transformers is needed
by the C++ runtime. Reference input generation is in
`tools/native_asr/feature_reference.py`; matrix fixtures are little-endian F32.
