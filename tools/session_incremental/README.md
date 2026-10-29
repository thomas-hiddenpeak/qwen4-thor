# Independent private snapshot delivery

Runtime: `Q4T_SESSION_INCREMENTAL=1` opts the private session cache into bounded
chunk reuse. `Q4T_SESSION_SAVE_TIMING=1` enables optional host timing. Both are off
by default. Public snapshots retain the installed packed format. No preallocation
or writer pipeline is included.

`prepare.py` freezes the one authorized 2026-10-29 delivery under
`.q4t-work/session-incremental-delivery-20261029`; the JSON holds the absolute
90-minute deadline, identities, fixed 48 measurement requests and six health
requests. Do not rerun expired plans or overwrite evidence. CPU storage runs once
after the first real evalscope HTTP service. `analyze.py` reports every cold and
resumed comparison. `archive.py` preserves complete raw output without size gates.

After acceptance, `deliver.py prepare` seals the package. `deliver.py deploy`
requires the shared exclusive guard and runs the complete health driver. A failed
health rolls back the known binary/sidecar pair; `finalize` additionally requires
the outer guard and final monitor boundary. Rollback is available through
`deliver.py rollback` under the guard; it refuses unknown installed identities.
No script creates a persistent service. The original research branch and failure
records remain at 4076810; this delivery is based on main dab8321.
