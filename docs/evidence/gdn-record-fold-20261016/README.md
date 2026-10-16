# GDN record/fold candidate evidence

This directory is an evidence archive, not an acceptance result.

- Original `http/`: **ENV_ABORT**.
- Second interrupted `http-clean/`: **ENV_ABORT**.
- Formal `http-resume/`: **HTTP_GATE_STOP**.
- Overall candidate acceptance: **not granted by this archive**.
- Numerical contracts: not adjudicated by this archiver.

The original attempt's `environment-abort.json` takes precedence over its
generic `gate-stop.json`; both are retained. It records zero HTTP requests
before interruption by unrelated concurrent HC diagnosis. An environment
abort is not a candidate NO_GO. The second `http-clean/` environment abort is
retained separately, including its generic gate-stop and request observations.
If `http-resume/` is absent, formal HTTP remains
NOT_RUN. An existing HTTP directory without a terminal marker is rejected.

`raw/recovered-quality/` preserves the recovery validation, clock evidence,
explanation and original DB identity inventory. Its empty recovered result set
does not turn the ten observed service-side completions into accepted quality
results: eleven quality cases still lacked durable HTTP evidence. The second
attempt records eleven service-side attempted requests, ten observed completed
and one cancelled; it is not described as a zero-request attempt. Files under
`recovered-quality/db-pristine/` and `db-recovery/`, including DB and journal,
are never copied into this archive. Their absolute paths, sizes and SHA-256
remain in `raw-manifest.json` for the raw workspace evidence.

HTTP_SCREEN_PASS_NUMERICS_PENDING means only that the HTTP driver finished
its frozen screen. It does not mean numeric contracts, full-model partial
fold, release acceptance, or overall PASS. HTTP_GATE_STOP keeps the original
failure reason and incomplete groups; no missing case is filled in.

Candidate source identity is the recorded `dcb2a7d07c6aba1c319d2c689c5e752c5616997d`, its per-run
manifest and patch under `raw/http*/identity/`. `frozen-source/` exports that
commit's patch and frozen plan. Current checkout HEAD is not substituted.
Per-group `commit.txt`/`worktree.patch` may identify the main HTTP harness,
not the isolated candidate source; use the explicit candidate identities.

`build_files_observed_at_archive` records size, SHA-256 and raw paths for
q4t, q4t_tests and q4t_mtp_lifecycle, plus CMakeCache. Binaries are not copied.
The equality fields distinguish current build observations from recorded run
identity; a current artifact is not automatically treated as the tested one.

`raw-manifest.json` freshly hashes all parent work evidence except the source
checkout, build intermediates and this output directory. Large databases,
requests and logs stay at their absolute raw paths. Small JSON, results,
responses, generated output text, drivers, identities, configure/build logs
and CMakeCache are copied when selected by the script's documented policy.
The manifest states exactly which files were copied.

Accepted baseline: `be74946d815904e84899c64fd88511cb0474166bef9d199c22330e8413db5072` at
`/home/rm01/models/dev/qwen4-thor/.q4t-work/mtp-numerical-closure-20261016/http`. `baseline/selected/` retains small result/identity metadata.
`baseline/historical-raw-manifest.json` is explicitly a historical inventory;
large baseline raw files were not freshly rehashed by this archive.

No builds, tests, HTTP requests, GPU work or acceptance decisions are performed
by the archiver. See `summary.json` and the original terminal/exit/comparison
files for the evidence actually recorded. `archive-results.py.txt` is copied
under `raw/` for reproducibility. Existing staging output is never overwritten.
