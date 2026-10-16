# Submitted-timestamp support question — prepared, not sent

This standalone reproducer contains no model, prompt, repository dependency,
or application workload. No external issue/message has been submitted.

## Observed environment

Jetson AGX Thor, SM110a; NVIDIA driver 595.78; CUDA driver API 13020;
CUPTI API 130300; CUDA13.3 compiler and headers; Linux aarch64.
The probe resolves libcupti.so.13 from CUDA13.3. No HES API is requested.
The use of mixed version numbers alone does not establish incompatibility.

## Reproduction

Build in a disposable directory (repository users must use build/ or
.q4t-work/), then execute only under the local GPU exclusivity policy:

```sh
/usr/local/cuda-13.3/bin/nvcc -std=c++17 -arch=sm_110a -ccbin g++-14 \
  -Xcompiler=-Wall,-Wextra repro.cu -lcupti \
  -Xlinker -rpath -Xlinker /usr/local/cuda-13.3/targets/sbsa-linux/lib \
  -o repro
```

Source calls cuptiActivityEnableLatencyTimestamps(1) at main entry before its
explicit CUDA calls, enables CONCURRENT_KERNEL activity, executes three
single-thread increments with cudaDeviceSynchronize after each, then normal
and forced flush on the same buffers. Check source for exact ordering.

Observed normal flush: no callback/records. Forced flush: one callback, three
kind10 records; queued/start/end nonzero and ordered, submitted=0 in all three.
Final device result3, dropped0, invalid3; program exits1 because the required
submitted timestamp is unknown. No errors from the CUPTI APIs were reported.
Full raw output is ../hc-up-submit-timestamps-20261017-v2/result.jsonl.
The original failure and targeted flush correction were not overwritten.

## Questions requiring confirmation

1. Is submitted supported on this Thor/driver/CUPTI combination under software
   tracing, and are there additional initialization or subscription requirements?
2. Is the shown API ordering sufficient, including any CUDA runtime static
   initialization, or is a different initialization hook necessary?
3. Is a matching documented defect/fix available? If not, is there a supported
   alternative that separates command submission from post-submission wait?

The source requests collection but does not prove every runtime internal
prerequisite is satisfied. Please distinguish documented restrictions from
suspected incompatibility. A proposed workaround must first pass this minimal
record-integrity check; do not jump directly to profiling a full model.

## Scope

This is a diagnostic-observability question, not a claim that CUPTI caused the
application regression. Original unprofiled HTTP comparisons did not use this
probe. Resolving this issue would enable a possible future causal design; it
would not itself establish the old regression's cause or accept the candidate.
