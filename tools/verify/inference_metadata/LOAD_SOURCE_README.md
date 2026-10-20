# Loading-source ownership regression

`q4t_load_source_test` is an independent CMake target. Without `--gpu` it
wraps uploads and delays reading host pointers until completion, including
copy and synchronization failures. It uses a synthetic safetensors file in
a new caller-supplied directory under `build/` or `.q4t-work/`.

```bash
cmake --build .q4t-work/megatron-reliability-20261020/build \
  --target q4t_load_source_test --parallel 8
.q4t-work/megatron-reliability-20261020/build/q4t_load_source_test \
  .q4t-work/new-load-host
```

The `--gpu` variant also checks real default and nonblocking streams. Run it
only under `tools/evalscope/gpu_exclusive.py`. The load-acceptance driver
freezes candidate binaries, source identities and existing quality fixtures,
then runs the upload test, MoE regression/parent comparison and off/on HTTP
quality. Its retained paths identify the 2026-10-20 experiment, not an arbitrary
future baseline. Do not silently reuse it for a different candidate/reference.

```bash
python3 -B tools/verify/inference_metadata/audit_load_acceptance.py \
  --stage .q4t-work/megatron-reliability-20261020
```

The offline audit requires successful guard and driver exits, verifies all
sealed files, quality results and MoE comparison, then compares the ordered
GPU function names and instruction/encoding hashes from saved SASS. It does
not run CUDA or establish performance acceptance.

`vision_load_test.cpp` separately loads the real vision tower, hashes each
host upload payload, then reads each GPU destination back after loading.
It checks loader integration and bytes, not vision semantics. Build command:

```bash
g++-14 -std=c++23 -Wall -Wextra -Werror -O2 \
  -Iinclude -I/usr/local/cuda/include \
  tools/verify/inference_metadata/vision_load_test.cpp \
  -L.q4t-work/megatron-reliability-20261020/build \
  -lq4t_model -lq4t_quant -lq4t_trace -lq4t_io \
  -L/usr/local/cuda/lib64 -lcudart -lcublasLt -lcublas \
  -Wl,--wrap=cudaMemcpyAsync -Wl,-rpath,/usr/local/cuda/lib64 \
  -o .q4t-work/megatron-reliability-20261020/vision_load_test
```

Run the executable with the read-only model directory as its only argument,
under its own complete GPU lease. Do not run it concurrently with HTTP/model
validation. Original source, executable and library hashes are retained in
`.q4t-work/megatron-reliability-20261020/vision-load-v1/plan.json`.
