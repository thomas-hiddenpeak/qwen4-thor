# Bounded pwrite tracing research tools

Status: **TOOL_UNAVAILABLE_INCOMPLETE_CORRELATION**, 2026-10-29. These tools
are a preserved research attempt, not an accepted GPU/HTTP collector. See
`docs/SESSION_PWRITE_TRACE_RESULT_2026-10-29.md` for the budget and evidence.
Do not rerun either storage fixture under the exhausted original budget.

The two actual CPU fixtures used 50,872,320 direct-I/O write bytes. Initial
compile and verifier failures are preserved separately and wrote no fixture
data. Neither the inference executable nor the delivered program changed.

## Scope and provenance

- `capability.bpf.c` / `capability.py`: initial attachment and stack probe.
- `trace.bpf.c` / `build_formats.py` / `collect.py`: 24-probe prototype and
  bounded ring-buffer reader, using libbpf through ctypes.
- `fixture.py`: second and final O_TMPFILE/O_DIRECT fixture, including two
  file lifetimes, fd/inode reuse and exact readback.
- `audit_fixture.py`: original count/byte audit. Its result is immutable;
  it is a necessary subset of the intended gate, not a complete contract.
- `review_capture.py`: offline salvage of the frozen second capture. It
  links setup command IDs to all 265 raw completions without replacing the
  failed gate or treating missing secondary records as lost device I/O.

The BPF source uses aarch64 pwrite64 number 68 and literal request/queue/disk
offsets for **6.8.12-1021-tegra**, device **259:0**. Offsets were extracted
from matching installed headers; source, object, bytes and compiler log are
in the stage archive. No kernel module was loaded. Tracepoint layouts and
the generated `trace_formats.h` are also bound in that archive. These
assumptions are not automatically validated by the prototype loader.

## Known incomplete contracts

FD/inode generations are provided by the fixture, not reconstructed from
open/close events. No complete parser tests cover merge, requeue, partial
completion, adversarial ordering, request/CID reuse and dropped events.
The fixture observed 265 raw NVMe completions but only 259 formatted
completions; zero ring-buffer loss does not prove all probes ran. All raw
NVMe events are inside a captured syscall, so post-syscall filtering alone
does not explain the six missing secondary records.

The collector retains tracked block requests across thread boundaries,
but formatted events use a global owned-pwrite-active filter. Filesystem
events can include other tasks, and missing insert events are not proof of
zero queue time. Request interval sums cannot be used as syscall wall time.
Ring order and event timestamp order differ in three places in this capture;
`start()` reserves a ring slot before recording its timestamp.

Only buffer/map limits were implemented; peak unified RAM and tracing
overhead were not measured. Flushing while the sampled active count is zero
is not a validated exclusion of all overlapping I/O. The prototype lacks
the complete phase driver, per-device background-I/O attribution, diskstats
and SMART boundaries required by the HTTP plan. No automatic model launch
is provided. The actual fixture completed with all links released and all
emitted records flushed; this does not validate every failure path.

## Offline replay only

From the repository root, with the existing preserved capture:

```bash
python3 -B tools/session_pwrite_trace/review_capture.py \
  .q4t-work/session-pwrite-trace-20261029/fixture-02
```

This reads existing files and prints JSON. It does not attach probes,
load a model or execute a storage fixture. Raw addresses/kallsyms remain
in the local full archive; the versioned summary contains symbolized stacks.
