# Separate data and control processes

Use `q4t-data.conf.in` and `q4t-control.conf.in` as two **separate nginx master
processes**, with different absolute `@PREFIX@` directories and different
`@FRONT_PORT@` values. Both point to the same private `@BACK_PORT@`. Combining
their server blocks into one worker would lose connection-pool isolation.

The local qualification tool renders all paths, validates the configurations,
starts the full runner and proxies, exercises saturation/reload/restart, and
stops everything afterward:

```sh
python3 tools/evalscope/run_isolated_control.py \
  --binary build/q4t \
  --nginx /absolute/path/to/nginx \
  --lifecycle .q4t-work/e2e/request-cancellation-v5-20260927 \
  --quality-run .q4t-work/e2e/listen-quality-20260927 \
  --output .q4t-work/e2e/isolated-control-new
```

Default evidence commands use backend 8000, data 8080, control 8081, all on
loopback. A fresh output directory is required. `--shared` instead qualifies
a single process against the same 128-connection test budget, reproducing
its control availability limit; it does not change the checked-in shared
profile's 512-connection value.

Routes are intentionally separate:

| Port role | Accepted paths | Other paths |
|---|---|---|
| Data | `/v1/chat/completions` | 404 |
| Control | `/v1/requests/cancel`, `/healthz`, `/metrics` | 404 |

Clients/gateways must send cancellation to the control address with the
original request ID and per-attempt credential. A 202 acknowledges cancellation;
it does not promise that an in-flight GPU chunk has already drained.

Each worker has 128 total connection slots, including upstream sockets.
Data generation admission remains 4; control admission is cancellation 8,
health 4, metrics 4. These are testable local budgets, not optimal production
capacity figures. An independent controller still cannot guarantee service
when the entire host, kernel, backend, or its own connection pool is exhausted.

The templates deliberately supply no public listener, TLS, authentication,
service installation or firewall rules. Loopback is not same-host tenant
isolation. A deployed control endpoint requires trusted access policy; do not
expose it simply because its generation route returns 404. This profile's
reload/restart and finite saturation checks are not long-soak or P99 proofs.
