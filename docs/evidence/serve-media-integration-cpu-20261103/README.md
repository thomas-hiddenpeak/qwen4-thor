# SRV-02i integration CPU evidence

Exact build/test logs, source snapshots, small PNG/JPEG fixtures, processor
pixels and candidate configuration are archived. Large ELF files and objects
stay local with identities, including the original failed probe executables.
The candidate and validation ELF, plus the previous default pair, are frozen.

The first CPU group failed one host test in each mode and the allocation
ledger probe. Only affected probes were corrected and rerun; unchanged
successes are reused. C-only injection now covers 27 distinct boundaries;
the old count 68 included 41 C++ calls and is not retained as C-only evidence.
The initial packaging attempt assumed missing public-host compile command
exports. That attempt is preserved; packaging was corrected without tests.

This is CPU acceptance only. vLLM still occupies the GPU; no model, listener,
GPU acceptance or default replacement occurred. Cooperative cancellation and
decoder-owned quotas do not prove preemption or a physical RAM hard limit.
