# Temporary cache-lock preparation research

This is a bounded reliability experiment, not a default startup path. The
candidate remains the sealed `startup-plan-20261020/order-candidate` binary.
See [the frozen protocol](../../../docs/STARTUP_CACHE_PREPARATION_2026-10-21.md).

`cgroup_observer.py` reads process birth identity and visible cgroup ancestors.
Missing root interfaces remain unknown; they are never treated as zero limits.
`locked_warm.py` owns read-only mappings in an unprivileged helper. Its exact
memlock limit belongs only to the helper; no persistent system setting changes.
Successful release requires zero owned mappings, VmLck zero, and normal exit.
The final exact cache gate occurs after that release, before the model starts.

`run.py` is an explicit derivative of the previous stage runner, kept separate
to preserve source identities bound to old evidence. It verifies history and
build identities, snapshots dependencies, holds the shared GPU lease, runs the
unchanged NVML guard and collectors, and seals every result. Each stage has one
preparation and one launch; a failed stage blocks the following stages.

The completed experiment ran these stages in order after its CPU contracts:

```sh
python3 -B tools/verify/startup_cache/run.py --stage mtp-lock-v3
python3 -B tools/verify/startup_cache/run.py --stage restart-lock-v3
python3 -B tools/verify/startup_cache/run.py --stage boundary-lock-v3
```

Do not rerun this completed matrix: its final cumulative budget of seven model
starts and five HTTP requests is exhausted. The new preparation work used five
preparations, five starts and four requests, including two interrupted starts.
An existing output directory is never reused. Both
trace-cap and ring-loss interruptions are preserved. v3 stops high-volume
kernel tracing, explicitly recording `kernel_timeline=NOT_COLLECTED`, while
keeping resource/NVML/application-ledger/HTTP/cleanup requirements mandatory.
All three v3 stages passed; no additional GPU attempt follows completion.
New artifacts go only under
`.q4t-work/startup-cache-20261021/`; prior sealed
directories remain read-only. The default binary is never replaced here.

The preparation changes reclaim history and physical layout. A successful
result supports only the tested preparation plus candidate configuration; it
does not isolate sorting benefit, prove a driver fix, or establish performance.
