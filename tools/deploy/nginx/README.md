# Local single-runner qualification profile

`q4t.conf.in` is a qualified local profile for a local nginx-to-runner hop.
It is **not an enabled service or a public deployment configuration**. The
front listener is loopback only. Render the three `@...@` markers using the
qualification tool; all logs and temporary bodies belong under its output
folder, not `/etc` or system service directories.

Generation requests share a global limit of 4 (including uploads and queued
requests). Cancellation has a separate limit of 8; health and metrics each
have their own limit of 4. Monitoring cannot consume the cancellation budget.
The key is the configured server name, not a caller-provided address or ID.
The profile intentionally uses bounded admission and returns 429 before an
excess generation request reaches the runner. These are qualification values,
not inferred production throughput or optimal queue sizes.

Request buffering stays on to keep slow uploads out of the runner. Response
buffering is off, client abort propagation is enabled, and automatic upstream
retry is disabled. The 600-second upstream inactivity timeout accommodates
the measured single-request 200K prefill; it is not a total deadline or a
promise that four queued 200K requests finish within that timeout. Select
queue limits, request deadlines and timeout budgets together for deployment.

Reproduce with an existing nginx binary (no root or service installation):

```sh
python3 tools/evalscope/run_proxy_admission.py \
  --nginx /absolute/path/to/nginx --binary build/q4t \
  --backend-host 127.0.0.1 \
  --lifecycle .q4t-work/e2e/request-cancellation-v5-20260927 \
  --quality-run .q4t-work/e2e/request-control-quality-20260927 \
  --performance-run .q4t-work/e2e/request-control-performance-20260927 \
  --output .q4t-work/e2e/proxy-admission-new
```

The output folder must be new. The tool occupies backend port 8000 and frontend
8080 from the prior command, starts a full model, and closes both processes at
completion. It records exact binaries/configuration, requests and responses.

Production prerequisites remain explicit:

- Prevent direct backend access. The accepted listen-address build defaults to
  loopback and supports `serve --host 127.0.0.1`; use the accepted binary
  identified in docs/STATUS.md. Older runners bind INADDR_ANY. This profile
  does not configure a firewall. Loopback does not isolate other local
  processes or shared-network-namespace tenants.
- TLS, authentication, tenant authorization and rate policy are separate work.
  Cancellation credentials do not authenticate the service.
- Header-incomplete clients and a flood against the control endpoints can
  exhaust nginx's own connection budget. Independent endpoint limits do not
  guarantee control availability during arbitrary connection/packet floods.
- Request-body buffering may use disk. Budget temporary-storage capacity and
  permissions; the profile's per-request maximum is not a whole-system quota.
- Multi-worker/multi-instance budgets, upstream routing, reload/restart, long
  soak and five-context proxy performance acceptance are not covered here.

Official directive semantics:
[proxy buffering, aborts and timeouts](https://nginx.org/en/docs/http/ngx_http_proxy_module.html),
[connection limits](https://nginx.org/en/docs/http/ngx_http_limit_conn_module.html).
`limit_conn` starts counting after complete headers, so it is not a complete
slow-header defense.

For independent data/control connection pools, see [the two-process profile](ISOLATED.md).
It uses separate nginx masters and a separate control port; it is not a drop-in
replacement for clients that send every API call to the data address.
