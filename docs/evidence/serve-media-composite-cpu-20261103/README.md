# Composite media preparation and CPU evidence

Frozen 32-generation / 21-negative / 11-start GPU plan, exact commands,
source bindings, build and CPU logs, compressed expansion fixture, and both
driver revisions are preserved. CPU checked 11 actual host cases and recovery;
the revised driver passed 13 offline cases. The unchanged fixture ELF was
reused after the control-flow review; its decompression was not repeated.

Production/validation candidates, two new probe ELFs, previous default pair
and source bundle remain local with identities. The device probe is built but
unexecuted. GPU preflight still sees the unrelated vLLM service, preserved as
ENV_ABORT without a test lease or service interference. No model/HTTP/GPU,
default replacement, physical RAM protection or whole-goal acceptance is claimed.
