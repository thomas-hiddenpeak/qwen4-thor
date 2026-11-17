# Native ASR development checks

Runtime code lives in `include/q4t/audio`, `src/audio`, and the ASR tokenizer
profile. Asset loading and full inference integration are still in progress;
these CPU checks do not establish that an audio HTTP endpoint is available.

`feature_reference.py` generates six synthetic PCM/log-mel fixture pairs using
the installed official Whisper feature extractor. It disables visible GPUs,
does not load model weights, and records source/version hashes. NumPy and
Transformers are **development-only** reference dependencies. For example:

```bash
.q4t-work/refenv/bin/python -B tools/native_asr/feature_reference.py \
  --preprocessor <pinned-assets>/preprocessor_config.json \
  --output .q4t-work/<stage>/feature-reference
```

Use a new output directory. Check all hashes before adopting generated
fixtures; never replace failed evidence to claim a pass. The explicit local
CMake targets `q4t_asr_contract_tests`, `q4t_asr_tokenizer_tests` and
`q4t_asr_feature_tests` support exact `--required-list` selection. The first
two require actual assets through `Q4T_ASR_ASSETS`; the tokenizer target also
uses the preserved embedding and main-model assets for direct regressions.
The public registry contains only metadata and synthetic feature checks.
