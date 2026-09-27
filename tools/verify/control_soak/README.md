# Bounded control-plane qualification

`tools/evalscope/run_control_soak.py` starts real nginx and a full runner,
using frozen lifecycle inputs. Supply `--binary`, `--nginx`, `--lifecycle`
and a fresh `--output` directory under `.q4t-work/`. The default minimum is
600 seconds and 12 mixed cycles; `--seconds` accepts 600–1800. A cycle contains
exact-output long and short requests plus active and queued cancellation.
Backend restart during prefill is tested after the loop. No synthetic model
stub or kernel benchmark is used.

`--baseline` expects the historical shared-control template, not the current
separated template. Use `--config-template` to select the archived
`q4t.conf.in` without changing the working configuration. The original
failing configuration and exact test script
are saved in `control-budget-baseline-20260927`; preserve that evidence.

All generated configurations, requests, normal responses, logs and resource
samples are stored under the output directory. Cancellation assertions and
nginx access logs additionally record the queued path. RSS is observational;
a bounded run is not proof of memory leak freedom or a production SLO.

The main loop uses a frozen minimum and a maximum admission time; an in-flight
HTTP request still has its own timeout. Cleanup and model reload are outside
the main-loop duration. The tool does not promise a hard whole-process deadline.

`seal.py` audits this dated delivery only after all writers exit. It checks
both baseline reproductions, saturation statuses, loop duration/resources,
restart results and each normal SSE output, then binds the exact binaries,
configurations and evidence files. Write seal stdout outside the sealed roots.
Do not modify archived experiments or reuse output directories for new runs.
