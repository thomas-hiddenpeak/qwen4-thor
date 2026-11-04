# SRV-02g evidence

The archive contains original HTTP bytes, logs, counters, raw CPU/native output,
resource samples and deliberate CUDA-status fault results. Faults are simulated
API return codes in a separate validation ELF, not physical OOM or GPU damage.
The production ELF has no environment-controlled fault injection.

`manifest.json` maps lossless compressed members to their original path and
SHA-256; ELF/object/source-bundle and private session snapshot identities remain
local. No blanket log redaction, complete allocation coverage, physical RAM
limit or sub-poll interference exclusion is claimed. The original FAILED unhanded-reservation probe expectation is preserved.
Native HTTP, actual MTP lifecycle and 16 independent probe successes are reused;
only the failed probe is repeated before the previously unstarted business cases.
The cumulative 18 probes explicitly exceed the original 17 by one.
