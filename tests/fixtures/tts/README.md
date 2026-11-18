# Fixed TTS metadata fixtures

Official configurations and safetensors headers for both native TTS profiles.
No weights or inference outputs are included. Header content is an independent
checkpoint shape/storage witness for the native graph builder, not a successful
model run or a full-file hash check. Revisions and file hashes: [sources.json](sources.json).
License/provenance: [Qwen3-TTS notice](../../../third_party/licenses/qwen3-tts/README.md).

`tokenizer-golden.json`是固定1.7B资产在官方Qwen2 tokenizer上的
16项CPU参考输出，记录Transformers/tokenizers版本及源码SHA。
无CUDA初始化或模型计算；实际vocab/merges通过Q4T_TTS_ASSETS
由本机已验完整资产提供，不将权重或重复的大词表放入仓库。
