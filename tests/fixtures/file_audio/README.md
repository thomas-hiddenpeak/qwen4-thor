# Synthetic audio codec inputs

Project-generated PCM and synthetic tones; no third-party recording or model
weights. See `tools/native_asr/codec_fixtures.py` and the exact generation
commands/asset digests in `provenance.json`. The fixed binaries are validation
inputs, not build products or runtime dependencies.

`mono16.wav` is one second of 440 Hz PCM16 at 16 kHz. `stereo24.wav` contains
440 Hz plus an 11 kHz component in two PCM24 channels at 48 kHz, so validation
can check averaging, phase and alias rejection independently. Float32/64 WAVs
use exactly representable fractional values. Compressed files encode the
synthetic tone via explicit CPU GStreamer plugins.

The original AAC encoding yielded only 14336 decoded samples, independently
reproduced byte-for-byte by the system decoder. `aac-input-padded.wav` adds
2048 explicit source silence samples for encoder priming/partial-frame
handling; updated AAC commands/digests are in `aac-provenance.json`. Original
failed fixtures remain in `.q4t-work/serve-audio-native-20261117/codec-fixtures-02`.
No output sample is fabricated by the decoder to make this fixture pass.

The first generation used a normal registry scan, whose unrelated NVIDIA
plugins reported CUDA device discovery failures. The current generator limits
discovery to named CPU plugins. GStreamer command-line tools are used only for
fixture generation; q4t uses verified system libraries inside its own worker.
