# Zero-hit static causal boundary (2026-10-24)

Source binding: runtime cb0fa73, unchanged in the new frozen plan. This note
was prepared without reading the ongoing diagnostic's performance results.

* `src/server/chat_server.cpp:283`: ON constructs a bounded token-class table
  before serving. It remains allocated until shutdown. Startup construction is
  outside request TTFT, but its persistent allocation and indirect allocator or
  cache effects are not automatically ruled out.
* `src/server/chat_generation.cpp:566`: the decode-stage event follows target
  prefill and precedes full-prompt draft extend. The first per-request suffix
  allocation is at line 706, after draft initialization, trunk cudaFree and
  scheduler registration. Current-request index work cannot directly account
  for earlier prefill elapsed time. Previous requests' released allocations,
  changed process layout, and machine state remain possible indirect paths.
* `src/mtp/suffix_drafter.cpp:112`: Init validates history, allocates an exact
  1 MiB trigram index and a capacity-sized global-ID history, initializes every
  index bucket, and inserts prompt tokens. There is no deferred allocation on
  query. With max_len=208896, payload is 1884160 bytes plus object and shared
  token classes; max allowed payload at 262144 is 2097152 bytes. Peak payload
  is not equivalent to measured total unified RAM.
* `src/server/chat_generation.cpp:738`: every speculative round clears ready;
  ON runs the host query before notifying the scheduler. This can delay GPU
  work admission and change scheduling even though its directly timed cost is
  small. ON increments acceptance counters before SSE emission, then appends
  committed tokens after SSE emission; neither operation adds target rows by itself.
* `src/server/chat_scheduler.cpp:360`: a rejected proposal supplies an empty
  span, identical to OFF. `src/mtp/mtp.cu:1386,1487` branches on that span, not
  the server switch. Thus a zero-hit request takes the same neural draft,
  verify, restore and accepted-prefix extend code paths, subject to its actual
  evolving state. No ON-specific CUDA launch or sync exists in this fallback.
  Static equality of branches is not a proof of arbitrary-input equal output
  or equal elapsed time; measured output, acceptance and round counts remain
  required evidence.
* `src/server/chat_generation.cpp:840`: Reset and diagnostic printing occur
  after speculative-loop unregistration. Host arrays are released explicitly;
  the same object's destructor sees empty pointers. No pooling or cross-request
  retained history is introduced. Allocator behavior after free is not proven
  by this ownership contract.

Distinguishing observations are prefill event vs client TTFT, actual MTP round
counts, native draft/verify/extend timing with rounding/window limits, and
periodic owned-thread waits/CPU frequencies/GPU frequencies/thermal/pressure
records. An unchanged OFF->OFF service control can expose same-identity drift
but does not prove a hardware mechanism or define an acceptable loss band.
No runtime candidate is proposed by this static audit alone.
