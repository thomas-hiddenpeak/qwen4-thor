# Nsight 2026.5.1 independent stage audit

- evidence_integrity: `PASS`
- evidence_validity: `ENV_ABORT`
- measurement_complete: `False`
- statistical_conclusion: `NOT_EVALUATED_DIAGNOSTIC_ONLY`
- engineering_decision: `NOT_ACCEPTED_TOOL_VALIDATION_ABORT`
- actual_http_requests: `0`
- actual_model_loads: `0`

Tool qualification and GPU development diagnostics only; no suffix performance acceptance or deployment. Explicit target NVLOG backend statements are separate from collection integrity; unknown backend remains UNKNOWN, absent export/schema remains UNOBSERVED. An absent independent target-exit receipt stays UNKNOWN even if a launcher or its parent has an exit code. Old controls and CPU/native/normal evidence remain separate. See JSON for source/package/raw identities, lifecycle, schema/metadata/diagnostics, pending, missing IO and ownership limits.
