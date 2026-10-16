# Targeted repair verification: final activity-buffer flush

The failed original probe used only cuptiActivityFlushAll(0). The local CUDA
13.3 header documents that this withholds any buffer containing an incomplete
record and recommends forced flush before session termination. This is a
specific lifecycle omission; do not infer hardware timestamp unavailability.

One new invocation with unchanged three synchronized increment kernels and
unchanged concurrent-kind target filter. Add counters for callbacks/all kinds,
report after normal flush then force-flush the SAME activity buffers without
launching more kernels. Retain original failure. Require value3, target records3,
no dropped or invalid timestamp records, queued<=submitted<=start<end.
Forced delivery alone does not make incomplete timestamps valid. If still zero
or invalid, stop and inspect records; do not repeat model runs. GPU exclusive
guard required. This is tool repair validation, not timing acceptance.
