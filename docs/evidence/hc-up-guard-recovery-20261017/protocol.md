# CPU guard / manager recovery integration — fixed protocol

Three cases, one execution each: normal guard completion; workload SIGKILLs
its guard/service main process; simulated restoration failure followed by
explicit simulated repair. Reuse real gpu_exclusive.run_guard and borrowed FD.
Only inspect() is replaced with an empty CPU-only monitor result. All lease
and configuration files are fake files within this directory. No GPU request,
real sysfs write, performance sample, or numerical acceptance.

Manager stores the lease OFD before fake configuration changes. ExecStopPost
must observe held lease, restore original configuration, and only then return.
TimeoutStopSec=infinity deliberately retains the lease while cleanup awaits
repair. In the injected failure case independently verify lease busy and
SubState=stop-post before permitting simulated repair. After all cases require
restoration, released lease, and transient unit collected. Normal and failure
cases must contain guard completion metadata without lease_released. Killed
guard has no completion record; independently recorded cleanup is required.

Limits: proves this machine's composition for these fixed failures only.
Does not cover cleanup process killed, manager failure, reboot, malicious
external sysfs writes, or production recovery privilege/journal integrity.
An indefinite stop handler alone is not persistent fail-closed protection for
these other cases. A durable quarantine gate remains necessary before real IO.
