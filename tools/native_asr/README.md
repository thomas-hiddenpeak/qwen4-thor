# Native ASR development checks

Runtime code lives in `include/q4t/audio`, `src/audio`, and the ASR tokenizer
profile. The fixed Qwen3-ASR-0.6B backend now performs real multipart JSON/text/SSE
transcription, with bounded codec/model resources and fault isolation.
Actual direct HTTP, pinned Python/JS SDK, native TLS, primary/light profiles
and retained/whole-payload recovery are qualified; reversible delivery is
next. CPU evidence alone never qualifies an audio endpoint.

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

`CMakeLists.txt` builds the isolated native `q4t_asr_replay` driver without
overwriting the published program. `prepare_cases.py` prepares pinned real
WAV clips using CPU reference preprocessing. `model_reference.py` verifies
the official source hashes and uses the explicitly documented encoder mask
adaptation. `acceptance.py` runs a bounded component comparison inside an
inherited strict GPU lease and protects the default release identities.

The first component comparison remains FAILED (five of eight logits exceed
the frozen cross-implementation thresholds). `decoder_isolation.py` and
`decoder_operand_review.py` investigate actual operands; their results do not
rewrite the original gate. The latter supports a layer interval and preserves
partial checks on exception. Its FP32/BF16 rounding and finite-ULP checks apply
to the captured instances, not every possible input. Original numerical evidence remains in `docs/evidence/serve-asr-compute-20261117`.
Real HTTP evidence is in `docs/evidence/serve-asr-http-20261117`, and managed
composition is in `docs/evidence/serve-asr-service-composition-20261117`.
Alignment/timestamps, speaker labels, VAD, Chat audio, speech synthesis and
Realtime remain required implementation tasks.

`codec_fixtures.py` generates synthetic PCM and compressed test inputs. It
uses a bounded list of CPU GStreamer encoder plugins only; the CLI is a
development tool, not a runtime dependency. `--only-aac` reproduces the
documented AAC source-tail correction without replacing passed Ogg/WebM
fixtures. Audio decoding itself lives in the isolated native server worker;
see `docs/evidence/serve-audio-codec-cpu-20261117` for original failures,
numerical boundary fixes and targeted CPU qualification.

`service_acceptance.py` freezes and runs bounded native TLS, retained-control
and whole-payload recovery checks under the shared strict GPU lease. Private
configuration is read directly from the operations listener.
`tls_composition.py` preserves both original failed TLS drivers while checking
their completed wire, cancellation, quota, recovery and cleanup observations;
its final health receipt comes explicitly from a separate same-engine managed
run. It never reclassifies an original failed continuous driver as passed.
