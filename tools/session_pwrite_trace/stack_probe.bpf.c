// CPU-only collision/failure validation; never used for HTTP observation.
#include "trace.bpf.c"
SEC("tracepoint/sched/sched_switch")
int stack_probe(struct sched* c) {
  if (!owned() || !c->state) return 0;
  long id = capture_stack(c);
  if (id < 0) { struct stats* s = stats();
    if (s) __sync_fetch_and_add(&s->stack_fail, 1); }
  struct event* e = start(50, now());
  if (e) { e->data[0] = id; submit(e, 0); }
  return 0;
}
