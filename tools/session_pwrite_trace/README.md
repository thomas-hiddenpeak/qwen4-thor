# Bounded pwrite tracing research tools

Current status: **ENV_ABORT / PARTIAL_DIAGNOSTIC_EVIDENCE**, 2026-10-29.
See [result](../../docs/SESSION_PWRITE_REPAIR_RESULT_2026-10-29.md) and
[plan](../../docs/SESSION_PWRITE_REPAIR_PLAN_2026-10-29.md). Budgets are exhausted;
these tools must not automatically launch another fixture or HTTP phase.
No inference runtime or default executable changed.

The 28 CPU correlation contracts and two direct-I/O fixtures passed. Missing
formatted NVMe events match kernel recursion_misses (24 and 7); raw lifecycle
records close the owned write bytes. lifecycle.py models file/FD, request and
CID generations, partial completion, requeue and interleaving. Missing wakeup
remains unknown; absent insert events do not prove zero block queue time.
Background FLUSH may have zero bytes; WRITE may not. Interval unions overlap.

run_http.py holds the shared GPU lease through the existing phase driver.
owned_collector.py adopts the privilege-dropped collector after its sudo
launcher exits; target.py binds the exact model PID before exec. The full
head completed 10 HTTP and two restores. The subset first request was cancelled
when stack helper failed; the phase remains ENV_ABORT. The actual captured
sources are separately frozen in the stage archive. No net benefit is claimed.

The error path originally lost its unflushed tail and stack map. finalize.py
now drains and persists tail/maps after detach, keeps FAILED, and avoids
re-appending accepted bytes after sync failure. Five targeted CPU checks pass,
including replay of the actual persisted HTTP prefix. This post-failure repair
has NOT had another live GPU/BPF validation; the helper errno itself is unknown.
Do not treat the collector as fully accepted or infer SSD-internal timings.

trace.bpf.c uses aarch64 pwrite64 number 68 and kernel structure offsets for
6.8.12-1021-tegra, device 259:0. The exact kernel, formats, object, source and
libbpf metadata are bound in the archive; portability is not established.
Original capability/audit/review tools and their failed results are retained.
Raw events are 128-byte records. Failure is not equivalent to every completed
file window being invalid: use independent identity/offset/byte/call closure,
while retaining global failed status and missing-tail limits.
