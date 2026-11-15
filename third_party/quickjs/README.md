# QuickJS core

Unmodified core files from the official QuickJS 2026-06-04 archive. Exact URL,
archive SHA256 and per-file identities are recorded in SOURCE.json. LICENSE is
MIT, copyright Fabrice Bellard and Charlie Gordon; retain it in distributions.

Only the core interpreter is embedded. The OS/filesystem module quickjs-libc,
command-line programs and dynamic module loader are not linked. q4t owns the
isolated worker, exposed tool capabilities and resource ceilings. The runtime
is instantiated per program replay; it has no service or package-manager
requirement. A worker failure must not affect unrelated model requests.
