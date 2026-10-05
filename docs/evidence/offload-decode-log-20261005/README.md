# Decode-log evidence — NO_GO

This packet accompanies the [final report](../../OFFLOAD_DECODE_LOG_2026-10-05.md). History remains a permanent veto; conditional delivery and a second candidate were not triggered. Archiving is not acceptance.

The tested source is `6b5169355d7aa666653c3b7076676e64f697d313`. The later archive commit is a separate identity, recorded by local post-commit receipts.

[package-manifest.json](package-manifest.json) binds final documents and all small packet payloads. Its [checksum](package-manifest.sha256) is detached; both and later receipts are excluded from its own entries.

[Raw index](raw-evidence-index.json) binds the full local manifest; raw entries/logs/resources/databases/build objects are not embedded. This Git packet is not a raw-data or model backup.

[Dependencies](dependencies.json) records tracked tools and external fixtures/core. [Controller source snapshots](../../../tools/evalscope/experiments/decode_log_20261005/) are exact bytes, non-portable and unsafe to rerun in place. All JSON archive paths resolve from the repository root.
