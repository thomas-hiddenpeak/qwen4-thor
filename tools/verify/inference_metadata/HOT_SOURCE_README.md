# MoE hot-transfer ownership acceptance

Scope and bounded budget:
[审计记录](../../../docs/MOE_HOT_UPLOAD_AUDIT_2026-10-20.md).
The default executable is not modified by this stage.

The retained stage uses `.q4t-work/moe-hot-upload-20261020/build`.
Configure Release with g++-14, CUDA host g++-14 and architecture 110a; build
`q4t q4t_tests q4t_host_tests q4t_load_source_test`. Two CPU-only fixtures
are compiled separately (no CUDA runtime linked):

```bash
g++-14 -std=c++23 -Wall -Wextra -Werror -O2 \
  -Iinclude -I/usr/local/cuda/include \
  tools/verify/inference_metadata/hot_storage_test.cpp \
  -o .q4t-work/moe-hot-upload-20261020/build/hot_storage_test

g++-14 -std=c++23 -Wall -Wextra -Werror -O2 \
  -Iinclude -I/usr/local/cuda/include \
  tools/verify/inference_metadata/batch_host_test.cpp \
  src/model/metadata_upload_batch.cpp \
  -Wl,--wrap=cudaEventCreateWithFlags -Wl,--wrap=cudaEventDestroy \
  -Wl,--wrap=cudaEventRecord -Wl,--wrap=cudaEventSynchronize \
  -Wl,--wrap=cudaStreamSynchronize -Wl,--wrap=cudaMemcpyAsync \
  -Wl,--wrap=cudaGetErrorString \
  -o .q4t-work/moe-hot-upload-20261020/build/batch_host_test
```

`hot_storage_test` defers D2H writes until a completion boundary. It covers
normal and null streams, early return after possible submission, failed waits
with destructor retry, and persistent failure retaining destinations. These
are controlled contract checks, not a reproduction of model corruption.
`batch_host_test` is the unchanged existing H2D snapshot/failure fixture.

The stage command below is the original run, not an instruction to overwrite
its evidence. A new attempt needs a fresh output directory and an explicit
reason; no automatic retries. All host checks also run inside this lease.

```bash
python3 -B tools/verify/inference_metadata/seal_stage.py \
  --output .q4t-work/moe-hot-upload-20261020/acceptance-v1 -- \
  python3 -B tools/verify/inference_metadata/run_hot_acceptance.py \
  --output .q4t-work/moe-hot-upload-20261020/acceptance-v1/run
```

The driver snapshots candidate binaries/cache and runtime sources, uses exact
required test names with no skips, executes three real-weight GPU tests, then
22 HTTP quality requests against sealed prior references. Partition tests set
explicit environment variables; normal quality runs strip experimental Q4T
variables. The outer sealer waits for the GPU guard to clean up its processes
before hashing the whole phase. Offline review:

```bash
python3 -B tools/verify/inference_metadata/audit_hot_acceptance.py \
  --stage .q4t-work/moe-hot-upload-20261020
```

The recorded source commit is the pre-change base; the source patch plus the
complete source snapshot/manifest identifies the actual tested implementation.
Timing observations do not constitute a paired performance optimization test.
The residency tests cover layer-level numerical contracts, not full-model
residency/offload service quality or device-context recovery.
