# One idle hardware capability test — frozen before first write

Purpose: confirm fixed GPC min/max interface and independent durable restoration
on this Thor. Exactly one transition 1575000000/1575000000 to
1260000000/1260000000, idle dwell 0.3 seconds, independent restoration. No model,
benchmark, HTTP request, or TTFT samples. This selects no accepted performance
configuration and establishes no OC causal relationship.

Use common repository GPU lease, real gpu_exclusive monitor, PID1 FD store,
ExecStopPost, durable ClockJournal at common .recovery-required path. Supervisor
and driver ordinary rm01 user; only sudo -n /usr/bin/tee writes fixed GPC range
files with validated integers and matching journal intent. No OC limit,
governor, CPU, NVD, EMC, runtime power, persistence, or model changes.

Before: freeze boot ID, resolved devices, governors, available frequencies,
NVD/EMC requested ranges and GPC range. Require original GPC 1575MHz,
NVD1692MHz, EMC4266MHz and no conflicts/marker. Identity checked before every
write. Require lowered min=max1260MHz, original final min=max1575MHz, other
frozen fields unchanged, guard COMPLETED, marker removed, lock available only
after cleanup, transient unit collected. cur_freq is reported separately and
is not actual transient hardware frequency proof.

Failure: no extra attempts. Any restore failure keeps ExecStopPost alive and
lease held (infinite stop timeout), records recovery-failed; if cleanup dies,
marker remains and blocks subsequent cooperative guards. Inspect and explicitly
repair under lease; never remove marker to bypass recovery. RuntimeMaxSec60
limits main. Previously passed CPU fault cases reused, not repeated on hardware.
