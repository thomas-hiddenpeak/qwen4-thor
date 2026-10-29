// CPU mocks exercise the actual probe branches; no BPF attach or storage I/O.
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "trace.bpf.c"

static struct stats observed;
static struct request saved_request;
static struct event last_event;
static u32 target_pid, normal;
static int has_request, emitted_count;
static void* mock_lookup(void* map, const void* key) {
  (void)key;
  if (map == &counters) return &observed;
  if (map == &target) return &target_pid;
  if (map == &normal_running) return &normal;
  if (map == &requests && has_request) return &saved_request;
  return 0;
}
static long mock_update(void* map, const void* key, const void* value, u64 flags) {
  (void)key; (void)flags;
  assert(map == &requests);
  memcpy(&saved_request, value, sizeof(saved_request));
  has_request = 1;
  return 0;
}
static long mock_read(void* out, u32 bytes, const void* source) {
  if ((uintptr_t)source < 4096) return -14;
  memcpy(out, source, bytes);
  return 0;
}
static u64 mock_time(void) { return 1234; }
static u64 mock_task(void) { return ((u64)100 << 32) | 100; }
static u32 mock_cpu(void) { return 0; }
static void* mock_reserve(void* map, u64 bytes, u64 flags) {
  (void)map; (void)flags;
  assert(bytes == sizeof(last_event));
  return &last_event;
}
static void mock_submit(void* event, u64 flags) {
  (void)event; (void)flags; ++emitted_count;
}
int main(void) {
  lookup = mock_lookup; update = mock_update; read_kernel = mock_read;
  now = mock_time; task = mock_task; cpu = mock_cpu;
  reserve = mock_reserve; submit = mock_submit;
  observed.active = 1;
  unsigned char rq[128] = {0}, queue[128] = {0}, record[128] = {0};
  u32 disk[2] = {259, 0};
  u64 pointer = (u64)(uintptr_t)queue;
  memcpy(rq, &pointer, 8);
  struct raw raw = {{(u64)(uintptr_t)rq, 0, 0, 0}};
  // Null gendisk is a valid admin queue, not a failed kernel read.
  for (u32 kind = 20; kind <= 22; ++kind) rq_event(&raw, kind);
  assert(observed.read_fail == 0 && !has_request && emitted_count == 0);
  normal_30(record); normal_31(record); normal_32(record);
  assert(observed.read_fail == 0 && emitted_count == 0);
  // Namespace data I/O still enters the tracked request path.
  pointer = (u64)(uintptr_t)disk; memcpy(queue+104, &pointer, 8);
  u32 bytes = 4096; memcpy(rq+44, &bytes, 4);
  rq_event(&raw, 11);
  assert(has_request && last_event.type == 11 && emitted_count == 1);
  memcpy(record+8, "nvme0n1", 8); normal_30(record); normal_31(record);
  memset(record, 0, sizeof(record)); memcpy(record+12, "nvme0n1", 8);
  normal_32(record);
  assert(emitted_count == 4 && observed.read_fail == 0);
  // A genuinely unreadable request still increments the fatal read counter.
  has_request = 0; raw.args[0] = 0; rq_event(&raw, 20);
  assert(observed.read_fail == 1 && emitted_count == 4);
  puts("PASS: null-gendisk raw/formatted excluded; namespace tracked; bad read fatal");
  return 0;
}
