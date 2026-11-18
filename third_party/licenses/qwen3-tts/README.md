# Qwen3-TTS fixed model metadata

Official source: [QwenLM/Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS/tree/022e286b98fbec7e1e916cb940cdf532cd9f488e), Apache-2.0; original [license](LICENSE) retained.

The configuration constants in `src/audio/tts_config_data.h` and fixtures in
`tests/fixtures/tts` preserve official Qwen model metadata. Sources:

- [CustomVoice 0.6B](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice/tree/85e237c12c027371202489a0ec509ded67b5e4b5)
- [CustomVoice 1.7B](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice/tree/0c0e3051f131929182e2c023b9537f8b1c68adfe)
- Their identical embedded speech tokenizer (12Hz, 24kHz output).

Weights are not distributed in this repository. Tensor headers are downloaded
metadata with recorded SHA256; a header alone does not verify the whole weight
file. Native tensor graph/validation code is independently implemented against
the published graph and metadata. No upstream Python runtime is bundled.
The qwen35-thor MIT implementation has only been reviewed at this stage.
