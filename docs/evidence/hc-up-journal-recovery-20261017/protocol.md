# Durable journal / guard / manager integration

Fixed four CPU cases, each once: normal, guard SIGKILL, restoration syscall
failure then explicit simulated repair, cleanup SIGKILL then guard rejection
and explicit simulated repair under the lease. All hardware IO is injected
fake-file IO. No NVIDIA polling or model inference. Uses actual ClockJournal
and run_guard; only guard.inspect replaced with a CPU-only stub.

Driver prepares durable .recovery-required journal only after guard acquired
lease and completed preflight; then lowers fake min/max. Independent cleanup
restores from persisted state before removing journal. Manager retains lease
through cleanup. For cleanup death, require manager termination, journal still
present, subsequent guard RECOVERY_REQUIRED without inspecting or executing,
then explicit simulated repair under newly acquired exclusive lease. All
cases must end at original fake range, no marker, and collected transient unit.

This adds lifecycle coverage missing from earlier fake-string recovery tests.
No actual sysfs, privilege boundary, hardware timing, or OC causal evidence.
