# Bounded pwrite tracing research tools

Current status: **ENV_ABORT / PARTIAL_DIAGNOSTIC_EVIDENCE**, 2026-10-29.
See [latest result](../../docs/SESSION_PWRITE_STACK_RESULT_2026-10-29.md) and
[budget](../../docs/SESSION_PWRITE_STACK_PLAN_2026-10-29.md). No further model,
HTTP, storage fixture or BPF attachment is authorized by the exhausted run.
The inference runtime and default executable did not change.

The original 28 CPU lifecycle contracts and five finalizer checks are reused.
Three new read-only contracts bind real BPF collision/failure captures. A
one-bucket test map yields real EEXIST; production uses 4096 buckets. On
EEXIST only, bpf_get_stack stores an immutable append-only fallback stack in
2048 x 256 bounded bytes. Other errors and exhaustion still fail. It does not
use REUSE_STACKID or FAST_STACK_CMP. The old incident errno remains unknown.

collect.py saves errno diagnostics independently of the ring. finalize.py
detaches, drains, saves pending events and stack maps, preserving FAILED.
A real abnormal HTTP exit preserved all 1,171,097 emitted records and 30 stacks.
This covers handled exceptions/signals, not SIGKILL, host crash or storage loss.
Allocated fallback indices are exported even when an earlier slot failed.

The only subset service admitted eight HTTP requests; five have completed
client/oracle evidence, zero restores ran. It stopped on 40 read errors from
null gendisk admin queues. Current raw and formatted probes exclude these
non-namespace commands; test_admin_scope.c exercises the actual C branches
with host mocks and retains fatal handling of genuinely invalid reads. This
post-failure fix compiled cleanly but has NOT been attached or HTTP validated.
The actual HTTP sources and post-failure sources are archived separately.

Full-head 10 HTTP/two restores and closed file windows from the prior stage
are reused. Neither partial run is promoted to a complete two-head pass.
Request/CID/FD generations, partial completion, requeue and exact byte closure
remain required. Missing insert does not mean zero queue time. The historical
parser conservatively counts preempted sched_switch states as missing wake;
latest offline analysis separates this host's state 256 from blocked state 2.
Interval unions overlap; issue-to-CQ is not SSD-internal time.

run_http.py supports the original 20/two-head scope or 10/subset-only scope;
the complete phase must run under tools/evalscope/gpu_exclusive.py.
owned_collector.py adopts the privilege-dropped child after sudo exits.
stack_probe.py is a CPU-only validation driver; its --audit-only mode replays
existing records without another attachment. stack_probe.bpf.c is never used
for HTTP. Raw records are 128 bytes; storage offsets are for aarch64 kernel
6.8.12-1021-tegra and device 259:0, not a portable ABI promise.
