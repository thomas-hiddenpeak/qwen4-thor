# pwrite trace capability evidence

Outcome: `TOOL_UNAVAILABLE_INCOMPLETE_CORRELATION`. No model/HTTP run.

- `budget.json`: original fixed deadlines and resource/request ceilings.
- `capability-01/`, `capability-01.log`: original verifier failure, zero writes.
- `capability-02/`: first actual fixture, nine attached/released links.
- `fixture-02/audit.json`: original failed capability decision; immutable.
- `fixture-02/offline-review.json`: later salvage of the same raw events,
  including usable raw completion chains and unresolved secondary gaps.
- `fixture-02/fixture.json`: exact write/readback fixture and file lifetimes.
- `fixture-02/exit.json`: flushed events, counters and released links.
- `formats.json`: local fixed tracepoint layouts and their digests.
- `postflight.json`: runtime/default identity, budgets, process cleanup and
  separate validity/completeness/statistical/engineering conclusions.
- `archive.json`, `files.json`: full local archive identity and per-file hashes.

The archive retains all raw events and kernel symbols, not just these
versioned summaries. No size threshold invalidated or removed any evidence.
See [result](../../SESSION_PWRITE_TRACE_RESULT_2026-10-29.md) for limitations.
