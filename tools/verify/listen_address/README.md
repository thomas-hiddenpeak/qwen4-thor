# Listen-address verification

Run against a candidate binary with a fresh evidence directory:

```sh
python3 tools/verify/listen_address/run.py \
  --binary .q4t-work/listen-address-control-20260927/candidate-q4t \
  --model-dir /absolute/read-only/model/path \
  --output .q4t-work/listen-address-new
```

This loads the full model three times, on port 8000. It checks the default,
explicit wildcard and explicit local-interface binding. The wildcard case
intentionally opens all IPv4 interfaces for the duration of that check.
Each server is terminated after its assertions, with exit status captured.
A global IPv4 address is required; the tool does not silently skip a case
when no such interface exists. It verifies socket ownership in `/proc` as
well as health responses and refused local-address connections. This is not
a remote-host firewall test or same-host tenant isolation proof.

CLI invalid-address/range tests point to a nonexistent model directory so
validation must fail before tokenizer loading. Numerical model quality and
five-context performance remain separate `tools/evalscope` checks.

`seal.py` is a one-shot audit for the named 2026-09-27 delivery directories.
Run only after the serial driver and both proxy/runner processes terminate;
write its stdout outside those directories. It binds source/binary identity,
checks exits, audits 26 evalscope rows plus four normal proxy SSE outputs,
and records the final file hashes. Do not mutate sealed evidence.
