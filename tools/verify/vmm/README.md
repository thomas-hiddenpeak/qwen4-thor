# Standalone device VMM reliability prototype

Not part of CMake production targets; never deploy these binaries as `build/q4t`.
Scope, frozen budget, outcome and caveats: `docs/VMM_PROTOTYPE_2026-10-20.md`.

Build from repository root (no GPU work):

```sh
mkdir -p .q4t-work/vmm-prototype-20261020
g++-14 -std=c++23 -Wall -Wextra -Werror -O2 \
  -I/usr/local/cuda-13.3/include tools/verify/vmm/probe.cpp \
  -L/usr/local/cuda-13.3/lib64/stubs -lcuda \
  -o .q4t-work/vmm-prototype-20261020/probe
g++-14 -std=c++23 -Wall -Wextra -Werror -O2 \
  -I/usr/local/cuda-13.3/include tools/verify/vmm/replay.cpp \
  -L/usr/local/cuda-13.3/lib64/stubs -L/usr/local/cuda-13.3/lib64 \
  -Wl,-rpath,/usr/local/cuda-13.3/lib64 -lcuda -lcudart \
  -o .q4t-work/vmm-prototype-20261020/replay
```

The frozen `allocations.txt` is the ordered `(api, bytes)` list of allocation
`begin` records in warm-start diagnostic-v2's sealed `server.log`.
`replay-plan.json` binds the source SHA and explicitly disclaims exact lifetime
reproduction. `audit.py` checks all 1493 entries against that source.
The completed experiment must not be rerun into existing output paths.
All GPU entrypoints require the outer `seal_stage.py` / `gpu_exclusive.py` lease.
Current diagnostic budget is exhausted; these instructions do not authorize
additional sampling. Audit is CPU-only:

```sh
python3 -B tools/verify/vmm/audit.py
```

`VmmBlock` is an explicit-lifetime prototype: callers must synchronize users,
call `Free`, and check results. It is not a general production allocator.
Handle-release failure aborts this diagnostic process rather than dropping
ownership silently. Partial unmapping failure retains remaining ownership.
Controlled create failure before chunk 0/2 and successful rounded allocation
were tested on GPU; full replay teardown was not reached due to ENV_ABORT.
