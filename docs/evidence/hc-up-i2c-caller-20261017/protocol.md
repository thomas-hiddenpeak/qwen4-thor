# One bounded passive I2C caller observation

Exactly one 300-second observation; no retries/extension, no model/HTTP/bench,
no I2C probing, no background service stop. Common GPU guard and real monitor
held throughout. Its nvidia-smi activity is part of the observed environment;
caller identification must not assume this observer is inert.

Create private tracefs instance, mono clock, 64KiB per CPU, i2c_read/write/result
filtered adapter_nr==0 only. Do not enable i2c_reply/read data recording. Save
trace, live process identities when available, event formats and per-CPU loss
stats. Disable and remove this instance on exit; global tracer untouched.

Positive attribution requires matched start/result on same thread with
identifiable caller, no losses, and a failed result overlapping a matching
kernel timeout report. Report the recorded transfer interval, not CPU busy
or model-stall time. Starts/results near capture edges may be unmatched and
must be explicitly excluded. An open FD alone is not attribution.

If no timeout, no usable matching pair, or loss occurs, preserve the outcome
and stop this capture family; do not run inference merely to provoke a match.
Even attribution here cannot retroactively identify historical callers or
prove TTFT impact. No clock/power or production implementation changes.
