# Qwen3-ASR-0.6B metadata fixtures

These are public configuration files and tensor metadata, not model weights.
Source: [Qwen/Qwen3-ASR-0.6B](https://huggingface.co/Qwen/Qwen3-ASR-0.6B/tree/5eb144179a02acc5e5ba31e748d22b0cf3e303b0),
revision `5eb144179a02acc5e5ba31e748d22b0cf3e303b0`, Apache-2.0.
The configuration files retain their original bytes. `tensor-header.json`
serializes the checkpoint's JSON header without its alignment whitespace.
It contains 612 BF16 tensors occupying 1,876,017,152 data bytes; both the
input embedding and output head are independent stored entries.

The complete checkpoint is 1,876,091,704 bytes, SHA256
`79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea`.
Local acquisition verifies every file against the fixed Git blob or LFS hash.
Public CPU tests use these fixtures; explicit `q4t_asr_contract_tests` also
requires the actual checkpoint via `Q4T_ASR_ASSETS`. Neither initializes CUDA.

See [source attribution and license](../../../third_party/licenses/qwen3-asr/README.md).

`tokenizer-golden.json` records 16 text/special-token cases from the frozen
official Qwen2 tokenizer implementation. `features/` contains six synthetic
PCM signals and their complete official Whisper log-mel matrices. Its manifest
records byte counts, SHA256, NumPy/Transformers versions, reference source
hashes and the 1e-5 maximum absolute error criterion. These fixtures contain no
user recordings or trained weights. They test the feature layer only.
