# Submission timestamp capability check, frozen before GPU execution

One invocation, three single-thread increment kernels, each followed by device
synchronization. No model, benchmark or timing acceptance. Common GPU exclusive
guard required. CUDA13.3 CUPTI explicit latency timestamps enabled before CUDA
initialization; concurrent kernel activity only, software tracing, no HES.
Require exactly three TimestampProbe records, no lost records, value=3,
nonzero queued/submitted and queued<=submitted<=start<end. Fail/unsupported
preserved with no retries. Do not interpret these trivial synchronized kernels
as evidence about model queue delays or attribution of the historical TTFT.

Purpose: distinguish unavailable data in existing SQLite from unsupported
hardware/API. Existing trace audit: no queued/submitted columns, QMD timing
columns null for all 2704554 records in each A/B trace. New full inference
capture is not authorized by this protocol and needs separate causal design.
