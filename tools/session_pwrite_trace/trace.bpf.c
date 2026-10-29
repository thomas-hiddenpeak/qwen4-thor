// Fixed host ABI; offsets are checked by the bounded CPU contract.
typedef unsigned long long u64;
typedef unsigned int u32;
typedef long long s64;
#define SEC(n) __attribute__((section(n), used))
#define U(name, value) int (*name)[value]
#define T(name, value) value* name
static void* (*lookup)(void*, const void*) = (void*)1;
static long (*update)(void*, const void*, const void*, u64) = (void*)2;
static long (*remove_key)(void*, const void*) = (void*)3;
static u64 (*now)(void) = (void*)5;
static u32 (*cpu)(void) = (void*)8;
static u64 (*task)(void) = (void*)14;
static long (*stackid)(void*, void*, u64) = (void*)27;
static long (*get_stack)(void*, void*, u32, u64) = (void*)67;
#ifndef STACK_ENTRIES
#define STACK_ENTRIES 4096
#endif
#ifndef STACK_RECOVER
#define STACK_RECOVER 1
#endif
struct stack_diagnostic { u64 attempts, first_errno, last_errno, recovered;
                          u64 fallback_count, fallback_error; };
struct { U(type, 2); U(max_entries, 1); T(key, u32);
         T(value, struct stack_diagnostic); } stack_errors SEC(".maps");
struct stack_value { u64 addresses[32]; };
struct { U(type, 2); U(max_entries, 2048); T(key, u32);
         T(value, struct stack_value); } extra_stacks SEC(".maps");
static long (*read_kernel)(void*, u32, const void*) = (void*)113;
static void* (*reserve)(void*, u64, u64) = (void*)131;
static void (*submit)(void*, u64) = (void*)132;
struct { U(type, 2); U(max_entries, 1); T(key, u32); T(value, u32); }
    target SEC(".maps");
struct stats { u64 active, emitted, lost, map_fail, stack_fail, read_fail, foreign_tail; };
struct { U(type, 2); U(max_entries, 1); T(key, u32); T(value, struct stats); }
    counters SEC(".maps");
struct { U(type, 27); U(max_entries, 16777216); } events SEC(".maps");
struct { U(type, 7); U(max_entries, STACK_ENTRIES); U(key_size, 4);
         U(value_size, 256); } stacks SEC(".maps");
struct { U(type, 6); U(max_entries, 1); T(key, u32); T(value, u32); }
    normal_running SEC(".maps");
struct operation { u64 generation, nr, args[3]; };
struct { U(type, 1); U(max_entries, 128); T(key, u32);
         T(value, struct operation); } operations SEC(".maps");
struct call { u64 generation, fd, offset, bytes; };
struct { U(type, 1); U(max_entries, 64); T(key, u32); T(value, struct call); }
    active SEC(".maps");
struct request { u64 generation, owner, call, fd, remaining; };
struct { U(type, 1); U(max_entries, 32768); T(key, u64); T(value, struct request); }
    requests SEC(".maps");
struct event { u64 ts, task, key; u32 type, cpu; u64 data[12]; };
static struct stats* stats(void) { u32 z = 0; return lookup(&counters, &z); }
static long read_checked(void* dst, u32 n, const void* src) {
  long rc = read_kernel(dst, n, src);
  if (rc) { struct stats* s = stats();
    if (s) __sync_fetch_and_add(&s->read_fail, 1); }
  return rc;
}
static int owned(void) {
  u32 z = 0; u32* p = lookup(&target, &z);
  return p && *p && (task() >> 32) == *p;
}
static int enabled(void) { struct stats* s = stats(); return s && s->active; }
static struct event* start(u32 type, u64 key) {
  u64 timestamp = now();
  struct event* e = reserve(&events, sizeof(*e), 0);
  struct stats* s = stats();
  if (!e) { if (s) __sync_fetch_and_add(&s->lost, 1); return 0; }
  __builtin_memset(e, 0, sizeof(*e));
  e->ts = timestamp; e->task = task(); e->cpu = cpu();
  e->type = type; e->key = key;
  if (s) __sync_fetch_and_add(&s->emitted, 1);
  return e;
}
struct enter { u64 common; s64 id; u64 args[6]; };
SEC("tracepoint/raw_syscalls/sys_enter")
int syscall_enter(struct enter* c) {
  if (!owned()) return 0;
  u32 tid = task();
  if (c->id != 68) {
    if (c->id == 56 || c->id == 57 || c->id == 23 || c->id == 24 ||
        c->id == 25 || c->id == 436 || c->id == 437) {
      struct operation op = {now(), c->id,
                             {c->args[0], c->args[1], c->args[2]}};
      if (update(&operations, &tid, &op, 0)) {
        struct stats* s = stats();
        if (s) __sync_fetch_and_add(&s->map_fail, 1);
      }
    }
    return 0;
  }
  struct call call = {now(), c->args[0], c->args[3], c->args[2]};
  struct stats* s = stats(); if (!s) return 0;
  if (update(&active, &tid, &call, 0)) {
    __sync_fetch_and_add(&s->map_fail, 1); return 0;
  }
  __sync_fetch_and_add(&s->active, 1);
  struct event* e = start(1, call.generation); if (!e) return 0;
  e->data[0] = call.fd; e->data[1] = call.offset; e->data[2] = call.bytes;
  submit(e, 0); return 0;
}
struct exiting { u64 common; s64 id, ret; };
SEC("tracepoint/raw_syscalls/sys_exit")
int syscall_exit(struct exiting* c) {
  if (!owned()) return 0;
  u32 tid = task();
  if (c->id != 68) {
    struct operation* op = lookup(&operations, &tid);
    if (op && op->nr == (u64)c->id) {
      struct event* e = start(6, op->generation);
      if (e) {
        e->data[0] = op->nr; e->data[1] = c->ret;
        e->data[2] = op->args[0]; e->data[3] = op->args[1];
        e->data[4] = op->args[2]; submit(e, 0);
      }
      remove_key(&operations, &tid);
    }
    return 0;
  }
  struct call* call = lookup(&active, &tid);
  if (call) {
    struct event* e = start(2, call->generation);
    if (e) { e->data[0] = c->ret; submit(e, 0); }
    remove_key(&active, &tid);
    struct stats* s = stats(); if (s) __sync_fetch_and_sub(&s->active, 1);
  }
  return 0;
}
static long capture_stack(void* context) {
  long id = stackid(context, &stacks, 0);
  if (id >= 0) return id;
  u32 zero = 0;
  struct stack_diagnostic* diag = lookup(&stack_errors, &zero);
  if (!diag) return id;
  __sync_fetch_and_add(&diag->attempts, 1);
  __sync_val_compare_and_swap(&diag->first_errno, 0, -id);
  diag->last_errno = -id;
  // Never overwrite a stack ID already referenced by an earlier event.
  if (STACK_RECOVER && id == -17) {
    u64 index = __sync_fetch_and_add(&diag->fallback_count, 1);
    if (index >= 2048) { diag->fallback_error = 28; return -28; }
    u32 key = index;
    struct stack_value* value = lookup(&extra_stacks, &key);
    if (!value) { diag->fallback_error = 2; return -2; }
    long bytes = get_stack(context, value, sizeof(*value), 0);
    if (bytes <= 0) {
      diag->fallback_error = bytes < 0 ? -bytes : 14;
      return bytes < 0 ? bytes : -14;
    }
    __sync_fetch_and_add(&diag->recovered, 1);
    return 4096 + index;
  }
  return id;
}
struct sched { u64 common; char comm[16]; u32 prev, prio; s64 state;
               char nextcomm[16]; u32 next, nextprio; };
SEC("tracepoint/sched/sched_switch")
int switched(struct sched* c) {
  u32 prev = c->prev, next = c->next;
  struct call* a = lookup(&active, &prev);
  if (a) {
    long id = c->state ? capture_stack(c) : -1;
    if (c->state && id < 0) { struct stats* s = stats();
      if (s) __sync_fetch_and_add(&s->stack_fail, 1); }
    struct event* e = start(3, a->generation);
    if (e) { e->data[0] = prev; e->data[1] = c->state;
      e->data[2] = id; submit(e, 0); }
  }
  a = lookup(&active, &next);
  if (a) { struct event* e = start(4, a->generation);
    if (e) { e->data[0] = next; submit(e, 0); } }
  return 0;
}
struct wake { u64 common; char comm[16]; u32 pid, prio, target_cpu; };
SEC("tracepoint/sched/sched_wakeup")
int woke(struct wake* c) {
  u32 pid = c->pid; struct call* a = lookup(&active, &pid);
  if (a) { struct event* e = start(5, a->generation);
    if (e) { e->data[0] = pid; submit(e, 0); } }
  return 0;
}
struct raw { u64 args[4]; };
static int rq_event(struct raw* c, u32 type) {
  u64 rq = c->args[0], q = 0, disk = 0, sector = 0;
  u32 major = 0, minor = 0, bytes = 0, flags = 0, tag = 0;
  struct request* r = lookup(&requests, &rq);
  if (!r && !enabled()) return 0;
  if (read_checked(&q, 8, (void*)(rq + 0))) return 0;
  if (read_checked(&disk, 8, (void*)(q + 104))) return 0;
  // Admin queues have no namespace gendisk; not a failed memory read.
  // They are outside the device 259:0 data-I/O attribution scope.
  if (!disk) return 0;
  read_checked(&major, 4, (void*)(disk + 0));
  read_checked(&minor, 4, (void*)(disk + 4));
  if (major != 259 || minor != 0) return 0;
  read_checked(&flags, 4, (void*)(rq + 24));
  read_checked(&tag, 4, (void*)(rq + 32));
  read_checked(&bytes, 4, (void*)(rq + 44));
  read_checked(&sector, 8, (void*)(rq + 48));
  if (!r && type != 10 && type != 11 && type != 20) {
    struct stats* s = stats();
    if (s) __sync_fetch_and_add(&s->foreign_tail, 1);
    struct event* tail = start(15, rq);
    if (tail) {
      tail->data[0] = type; tail->data[1] = bytes;
      tail->data[2] = flags; tail->data[3] = sector;
      submit(tail, 0);
    }
    return 0;
  }
  if (!r) {
    struct request v = {now(), 0, 0, 0, bytes};
    u32 tid = task(); struct call* a = lookup(&active, &tid);
    if (owned() && a) { v.owner = task(); v.call = a->generation; v.fd = a->fd; }
    if (update(&requests, &rq, &v, 0)) { struct stats* s = stats();
      if (s) __sync_fetch_and_add(&s->map_fail, 1);
      return 0;
    }
    r = lookup(&requests, &rq); if (!r) return 0;
  }
  if (type == 11) r->remaining = bytes;
  struct event* e = start(type, rq);
  if (e) {
    e->data[0] = r->generation; e->data[1] = r->owner;
    e->data[2] = r->call; e->data[3] = bytes; e->data[4] = sector;
    e->data[5] = flags; e->data[6] = tag;
    if (type == 12) { e->data[7] = c->args[2]; e->data[8] = c->args[1]; }
    if (type == 20) read_checked(&e->data[7], 32, (void*)c->args[1]);
    u32 z = 0; u32* normal = lookup(&normal_running, &z);
    if (normal) e->data[11] = *normal;
    submit(e, 0);
  }
  if (type == 14) remove_key(&requests, &rq);
  if (type == 12) {
    if (c->args[2] >= r->remaining) remove_key(&requests, &rq);
    else r->remaining -= c->args[2];
  }
  return 0;
}
#define RQ(fn, point, kind) SEC("raw_tracepoint/" point) \
  int fn(struct raw* c) { return rq_event(c, kind); }
RQ(inserted, "block_rq_insert", 10)
RQ(issued, "block_rq_issue", 11)
RQ(completed, "block_rq_complete", 12)
RQ(requeued, "block_rq_requeue", 13)
RQ(merged, "block_rq_merge", 14)
RQ(nvme_setup, "nvme_setup_cmd", 20)
RQ(nvme_complete, "nvme_complete_rq", 21)
RQ(nvme_cq, "nvme_sq", 22)

// Generated wrappers copy only the recorded fixed portion of each format.
#include "trace_formats.h"
char q4t_license[] SEC("license") = "GPL";
