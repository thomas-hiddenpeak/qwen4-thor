# Request-policy evidence — completed performance NO_GO

This package records the 2026-10-04/05 bounded request-length policy phase.
Both the mixed-history screen and the six-tier matrix produced valid NO_GO.
The default remains off; conditional business/lifecycle tests and a second
optimization were not triggered. A commit or archive is not acceptance.

The tested runtime is source `52b42b2e7775929ed9cd6a237c822bc6dfa87a06`,
with direct parent diagnostic entry `f3e3d21`, and binary SHA256
`34eb4cf5b53dfcc5b0783120fcdec438fd321dbd4c981f52eee581c96f7fb93a`.
The documentation/archive commit containing this packet is a separate identity.
See the [phase report](../../OFFLOAD_REQUEST_POLICY_2026-10-04.md).

## Decisions and scope

Read the original [plan](records/plan.json),
[execution plan](records/execution-plan.json) and
[prospective coverage amendment](records/coverage-amendment.json) together.
The amendment was frozen before candidate history performance collection, after
candidate quality had completed. It permits the original six tiers after a valid
history speed rejection while retaining the history failure as a permanent veto.
It does not change samples or gates. Original stop fields remain historical.

The first HTTP quality set passed 11 cases, host/tools passed 139 checks and the
three selected real-weight numerical contracts passed. These are bounded
quality and layer/shape checks, not whole-model numerical or production-quality
certification. Completed evidence was reused without additional model samples.

Both history arms completed 21 requests; both matrix arms completed 18 requests.
All response/usage/capacity/identity and recorded path/cleanup contracts passed.
Short lengths failed speed gates; long-length gains do not cancel those failures.

- [Report content review](records/final-report-independent-review.json)
- [History independent review](records/history-independent-review.json)
- [Six-tier independent review](records/matrix-independent-review.json) and
  [descriptive summary](records/matrix-summary.json)
- [Overall performance decision](records/performance-decision.json)
- [Independent resource review](records/resource-final-independent-review.json)
  and [resource summary](records/raw-resource-summary.json)
- [Protection check](records/final-protection.json),
  [first failed invocation](records/final-protection-execution/exit.json),
  [specific offline repair](records/final-protection-preparation-v2.json) and
  [successful second invocation](records/final-protection-execution-v2/exit.json)

The first protection checker refused three non-dumpable same-user process exe
links. Its failure is preserved. The reviewed repair used read-only privileged
readlink with PID start-time checks; no model test was repeated for this issue.
Protection has explicit snapshot/metadata/reference-tree limits. It does not
certify continuous whole-machine immutability or whole-device memory release.

Resource coverage is five completed groups, 89 HTTP requests in 79 client
observation envelopes. Quality 11 is one batch envelope. Performance groups use
16GiB host/cache cgroups, while quality has MemoryMax=max. NVIDIA/PID/cgroup/
partition/device counts remain separate. PSI, live cache peaks and the deduplicated
54GB whole-machine physical-memory limit remain unknown. Read the summaries for
counter disagreement, missingness and actual observation windows.

## Package and raw evidence inventories

[package-manifest.json](package-manifest.json) is the final small-payload
inventory, with a [detached checksum](package-manifest.sha256). It includes the
final documents and packet payloads; it excludes itself, its checksum and later
post-commit receipts to avoid recursive hashes. Local post-commit delivery and
completion receipts separately record the actual local/remote Git identities.

Earlier inventory snapshots remain unchanged:

- [Original 91-copy staging snapshot](manifest.json)
- [Completed baseline additions](baseline-additions-manifest.json)
- [Terminal/protection additions](terminal-additions-manifest.json)
- [Final review additions](final-review-additions-manifest.json)

Their past PENDING/staging fields describe when they were written; they are not
current routing or acceptance instructions. Their original-byte provenance is
retained and the final package inventory covers all current small payloads.

The [raw evidence index](raw-evidence-index.json) binds the full local raw
manifest by path, count, size and SHA. Raw manifest entries are not all embedded
in this Git package. Full HTTP/server logs, JSONL/CSV, databases, source tar and
binaries remain local; a SHA does not reconstruct absent data. This packet is not
a full raw-data or model backup. Nothing was compressed, removed or deduplicated
as part of packaging. Model payloads and the reference tree are not bundled.

## Archived sources and reproducibility limits

The [controller snapshot directory](../../../tools/evalscope/experiments/request_policy_20261004/)
contains exact original source bytes and the immutable resource core. Do not
import or run archived copies: they bind absolute original paths, tested identities
and one-shot outputs, and some act at import time. A new experiment needs a new
directory and newly frozen commands; never overwrite these records.

The two new boundary prompts are preserved. [boundary-fixtures.json](boundary-fixtures.json)
records generator, seed, original commands and SHA; actual server usage supplies
the runtime length evidence. Tokenizer/model/environment dependencies remain
external. [dependencies.json](dependencies.json) binds already tracked tools to
the tested commit and SHA, without duplicating them.

JSON archive_path and repository_path fields resolve from the repository root.
During preparation archive paths resolve below delivery-staging; after copying,
all listed small payloads resolve in the isolated repository checkout. External
absolute origins remain provenance, not portable paths or automatic rerun entrypoints.
