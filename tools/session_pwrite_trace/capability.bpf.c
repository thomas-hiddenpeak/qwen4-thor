// Fixed tracepoint capability gate; no kprobe or CO-RE dependency.
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
static u64 (*task)(void) = (void*)14;
static long (*stackid)(void*, void*, u64) = (void*)27;
static long (*read_kernel)(void*, u32, const void*) = (void*)113;
struct { U(type, 2); U(max_entries, 1); T(key, u32); T(value, u32); }
    target SEC(".maps");
struct counts { u64 calls, sleeps, stack_ok, stack_fail, blocks, completes;
                u64 bytes, bad_device, raw_nvme, nvme, fs_enter, fs_exit;
                s64 last_stack_error; };
struct { U(type, 2); U(max_entries, 1); T(key, u32); T(value, struct counts); }
    counters SEC(".maps");
struct { U(type, 7); U(max_entries, 1024); U(key_size, 4);
         U(value_size, 256); } stacks SEC(".maps");
struct { U(type, 1); U(max_entries, 64); T(key, u32); T(value, u64); }
    active SEC(".maps");
struct { U(type, 1); U(max_entries, 8192); T(key, u64); T(value, u64); }
    requests SEC(".maps");
static struct counts* counts(void) { u32 z = 0; return lookup(&counters, &z); }
static int owned(void) {
  u32 z = 0; u32* p = lookup(&target, &z);
  return p && *p && (task() >> 32) == *p;
}
struct enter { u64 common; s64 id; u64 args[6]; };
SEC("tracepoint/raw_syscalls/sys_enter")
int syscall_enter(struct enter* c) {
  if (owned() && c->id == 68) {
    u32 tid = task(); u64 t = now(); update(&active, &tid, &t, 0);
    struct counts* s = counts(); if (s) __sync_fetch_and_add(&s->calls, 1);
  }
  return 0;
}
struct sched { u64 common; char comm[16]; u32 prev, prio; s64 state;
               char nextcomm[16]; u32 next, nextprio; };
SEC("tracepoint/sched/sched_switch")
int switched(struct sched* c) {
  u32 prev = c->prev;
  if (lookup(&active, &prev) && c->state) {
    struct counts* s = counts(); if (!s) return 0;
    __sync_fetch_and_add(&s->sleeps, 1);
    long id = stackid(c, &stacks, 0);
    if (id >= 0) __sync_fetch_and_add(&s->stack_ok, 1);
    else { __sync_fetch_and_add(&s->stack_fail, 1); s->last_stack_error = id; }
  }
  return 0;
}
struct raw { u64 args[4]; };
SEC("raw_tracepoint/block_rq_issue")
int block_issue(struct raw* c) {
  if (!owned()) return 0;
  u64 rq = c->args[0], q = 0, disk = 0;
  u32 major = 0, minor = 0, bytes = 0;
  // Exact offsets generated from this host's matching kernel headers.
  read_kernel(&q, 8, (void*)(rq + 0));
  read_kernel(&disk, 8, (void*)(q + 104));
  read_kernel(&major, 4, (void*)(disk + 0));
  read_kernel(&minor, 4, (void*)(disk + 4));
  read_kernel(&bytes, 4, (void*)(rq + 44));
  struct counts* s = counts(); if (!s) return 0;
  if (major != 259 || minor != 0) {
    __sync_fetch_and_add(&s->bad_device, 1); return 0;
  }
  u64 value = bytes; update(&requests, &rq, &value, 0);
  __sync_fetch_and_add(&s->blocks, 1);
  return 0;
}
SEC("raw_tracepoint/block_rq_complete")
int block_complete(struct raw* c) {
  u64 rq = c->args[0];
  if (lookup(&requests, &rq)) {
    struct counts* s = counts(); if (!s) return 0;
    __sync_fetch_and_add(&s->completes, 1);
    __sync_fetch_and_add(&s->bytes, c->args[2]);
    remove_key(&requests, &rq);
  }
  return 0;
}
struct exiting { u64 common; s64 id; s64 ret; };
SEC("tracepoint/raw_syscalls/sys_exit")
int syscall_exit(struct exiting* c) {
  if (owned() && c->id == 68) {
    u32 tid = task(); remove_key(&active, &tid);
  }
  return 0;
}
SEC("raw_tracepoint/nvme_setup_cmd")
int nvme_raw(struct raw* c) {
  (void)c; if (owned()) { struct counts* s = counts();
    if (s) __sync_fetch_and_add(&s->raw_nvme, 1); }
  return 0;
}
SEC("tracepoint/nvme/nvme_setup_cmd")
int nvme_record(void* c) {
  (void)c; if (owned()) { struct counts* s = counts();
    if (s) __sync_fetch_and_add(&s->nvme, 1); }
  return 0;
}
SEC("tracepoint/ext4/ext4_ext_map_blocks_enter")
int fs_enter(void* c) {
  (void)c; if (owned()) { struct counts* s = counts();
    if (s) __sync_fetch_and_add(&s->fs_enter, 1); }
  return 0;
}
SEC("tracepoint/ext4/ext4_ext_map_blocks_exit")
int fs_exit(void* c) {
  (void)c; if (owned()) { struct counts* s = counts();
    if (s) __sync_fetch_and_add(&s->fs_exit, 1); }
  return 0;
}
char q4t_license[] SEC("license") = "GPL";
