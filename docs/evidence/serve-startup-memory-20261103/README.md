# SRV-02e evidence

Original failure and targeted correction are separate. `original-result.json`
remains FAILED; `result.json` reuses five successful model turns and the corrected
three-worker accounting, then adds only the four previously unexecuted probes.

`raw-records.tar.gz` contains lossless original bytes. `manifest.json` maps each
member to its original path/SHA-256; executable/object/source-bundle identities
are retained locally. The archive includes original HTTP, logs, counters and
failure records, without a blanket redaction claim. No files were synthesized
for the unexecuted final health probes. GPU polling cannot exclude subinterval
interference. The measurements do not establish a whole-machine RAM limit.
