# Bounded cuFile probe

`probe.cpp` is a read-only availability and byte-correctness diagnostic for the
installed CUDA 13.3/cuFile 1.18 API. It is not a throughput test or a model runtime
change. Thor host and device allocations both consume the same physical RAM;
see [the SSD capacity contract](../../../docs/SSD_OFFLOAD_CONTRACT.md).

The frozen 2026-10-21 run is complete: two strict initializations failed; two
compatibility reads matched the source and reported POSIX, with no P2P/NVFS
reads. The requested 10 MiB GPU slab setting was not confirmed: the library
reported its default 384 MiB pool configuration. This is not proof that the
whole pool was allocated. Treat `run.py` as the preserved experiment driver,
not an accepted memory-limited backend; resolve effective pool configuration
before a future run. See [the result](../../../docs/GDS_MINIMAL_EXPERIMENT_2026-10-21.md).

Build only (this command does not execute the binary):

```sh
g++-14 -std=c++23 -O2 -Wall -Wextra -Werror \
  -I/usr/local/cuda/include tools/verify/gds/probe.cpp \
  -L/usr/local/cuda/lib64 -Wl,-rpath,/usr/local/cuda/lib64 \
  -lcufile -lcudart -lcuda \
  -o .q4t-work/gds-minimal-20261021/build/probe
```

Invocation contract, exclusively inside the owning GPU lease and external
timeout (even an argument-only invocation must stay inside that lease):

```text
probe --path READ_ONLY_FILE --offset DECIMAL --bytes DECIMAL
      [--register-buffer] [--hold-seconds 0..3]
```

The required interval must fit an existing regular file. `bytes` is 1..8 MiB;
the requested file offset and size are passed through unchanged, including
unaligned values. The allocation is rounded up to 4 KiB and its actual address
alignment is reported. Device ordinal is 0. No writes or configuration changes
are made to the source file or system. cuFile configuration is supplied by the
owning runner; the probe changes only its process statistics collection level.

The probe opens the file using `O_RDONLY|O_DIRECT`, allocates at most 8 MiB of
device storage and two ordinary host buffers totaling at most 16 MiB, opens
cuFile, registers the file handle, and optionally registers the device buffer.
A requested buffer registration failure fails the case without retrying an
unregistered path. There is exactly one `cuFileRead` attempt, with no retry or
application fallback. A short read fails. After an exact read, D2H is followed
by one ordinary `pread` of the same interval, then a byte-by-byte comparison.
This truth read happens after the cuFile read and can itself warm the file.
Device/inode/size/mtime/ctime are checked to reject a changing source; this is
not a cryptographic integrity claim about an adversarial writer.

Stdout is flushed JSONL with monotonically increasing `seq`, PID and `mono_ns`:

- `api`: return code, error description and elapsed microseconds for each API.
  `phase` distinguishes work, optional diagnostics and cleanup. The handle
  deregistration API returns `void`, explicitly represented by `rc:null`.
- `configuration`: effective values queried after `cuFileDriverOpen`, including
  `force_compat_mode`, `allow_compat_mode`, `use_pcip2pdma` and buffer limits.
  Unknown values are `null`; configured limits are not measured RAM usage.
- `stats`: before/after read L3 snapshots, including fixed-width hex GPU UUIDs,
  read bytes and total/P2PDMA/NVFS/POSIX/unaligned/error counters. Collection
  setup or getter errors produce `known:false`; zeroes are never substituted
  for failed diagnostics. Counter categories may overlap. Actual library pool
  memory is not exposed by this API and remains unknown.
- `before_read`: emitted before the read and immediately flushed.
- `process_io`: `/proc/self/io` snapshots around only `cuFileRead`, excluding
  the later truth read. Deltas include possible observer/library I/O and are
  process-level observations, not expert-exclusive SSD attribution. Unknown or
  decreasing counters produce `null` deltas.
- `ready`: emitted only after an exact read, matching truth bytes and unchanged
  source metadata. Device/host buffers and cuFile registration remain alive
  during the optional hold, bounded at 3 seconds.
- `result`: `status`, `failure`, `read_attempted`, `returned_bytes`,
  `requested_bytes`, `data_match`, `file_unchanged`, `cleanup_ok`, `logging_ok`
  and `signal`. `PASS` requires the read/compare and cleanup to succeed.

`native_path` is always `UNPROVEN`, and `performance_accepted` is always false.
The runner must independently interpret effective configuration and actual
path counters. Allowing compatibility mode does not prove that POSIX was used;
successful reads, DMA-BUF support, configured P2P flags or zero POSIX counters
alone do not prove native GDS. This probe does not test total RAM savings,
MoE correctness, cancellation of asynchronous I/O or model E2E performance.

SIGINT/SIGTERM cause cleanup at the next ordinary API boundary or interrupt the
hold. There is no safe in-process cancellation guarantee for a stalled driver
call; the owning runner remains responsible for the deadline and process
cleanup. Cleanup errors are reported and fail the case. Uncatchable termination
cannot produce a successful final result. The probe never retries the read.
