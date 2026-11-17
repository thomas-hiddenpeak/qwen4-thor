# Qwen3-ASR source and metadata attribution

Qwen3-ASR: Copyright 2026 The Qwen team, Alibaba Group and the HuggingFace Inc.
team. All rights reserved. Licensed under the [Apache License 2.0](LICENSE).

- Official code reference: [QwenLM/Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR/tree/7c6daf77a2421100f5fb066495372c00129d39ff).
- Model metadata: [Qwen/Qwen3-ASR-0.6B](https://huggingface.co/Qwen/Qwen3-ASR-0.6B/tree/5eb144179a02acc5e5ba31e748d22b0cf3e303b0).
- Current use: public configuration/tensor-header fixtures, ASR added-token
  metadata, and independently written C++ architecture/weight validation,
  tokenizer loading, log-mel, convolution/window encoder and dense decoder
  execution. The decoder reuses this project's native Qwen3 embedding kernels.
  No model weights or
  Python inference implementation are distributed in this source tree. The
  original configuration bytes are retained; the safetensors header is
  reserialized as fixture JSON.
- Native transcription is delivered; actual forced alignment remains in
  progress. Metadata/tokenizer tests alone do not prove timestamp accuracy.

The original license is included unchanged. The fixed official code was
reviewed for the computation contract. Development-only `model_reference.py`
loads that exact official implementation from a separately verified checkout.
It supplies the block-diagonal mask derived from `cu_seqlens` to encoder eager
attention, which the pinned forward path does not otherwise pass. This is an
explicit reference adaptation, not a claim of unchanged eager/FA2 equivalence.
The model reference uses isolated Transformers 4.57.6 and PyTorch 2.14.0+cpu;
its dependencies and imported Python implementation are not core dependencies.

Tokenizer/feature CPU reference: Transformers 5.16.1, tokenizers 0.23.2;
the exact installed source hashes accompany the fixtures. The reference
`transformers.audio_utils` is Copyright 2023 The HuggingFace Inc. team and the
librosa & torchaudio authors, Apache-2.0. The C++ feature path implements
standard DFT/Hann/Slaney equations independently; the reference runs only to
generate synthetic validation data. No Python, NumPy or Transformers is needed
by the C++ runtime. Reference input generation is in
`tools/native_asr/feature_reference.py`; matrix fixtures are little-endian F32.

Forced-alignment metadata and tokenizer use the same source project and
[Qwen/Qwen3-ForcedAligner-0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B/tree/c7cbfc2048c462b0d63a45797104fc9db3ad62b7),
also Apache-2.0. The added non-special `<timestamp>` token and 24-layer encoder /
5000-class head contracts are implemented natively; fixture headers contain no
weights. The staged asset is optional and no Python runtime is distributed.

`src/audio/aligner_postprocess.cpp` adapts Unicode text cleaning and the stable
longest-nondecreasing-subsequence timestamp repair from
`qwen_asr/inference/qwen3_forced_aligner.py` at the fixed source revision above.
Copyright 2026 The Qwen team, Alibaba Group and the HuggingFace Inc. team.
Changes: C++ implementation, explicit bounds/cancellation and atomic output,
original UTF-8 spans, raw/repaired/clipped accounting and duration checks.
Japanese/Korean segmentation uses the already-linked ICU word iterator instead
of Nagisa/SoyNLP; equivalence and real-language accuracy are not asserted.
No Python segmentation package or reference inference runtime is shipped.
