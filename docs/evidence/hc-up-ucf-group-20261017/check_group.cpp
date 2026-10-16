#include <linux/perf_event.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <array>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <time.h>
int main() {
  constexpr std::array<uint64_t, 4> configs{0x100000000ULL, 0, 6, 12};
  std::array<int, 4> fds{-1, -1, -1, -1};
  std::array<uint64_t, 4> ids{};
  auto fail = [&](const char* operation) {
    const int error = errno;
    for (int fd : fds) if (fd >= 0) close(fd);
    std::printf("{\"error\":\"%s\",\"errno\":%d}\n", operation, error);
    return 1;
  };
  for (size_t i = 0; i < configs.size(); ++i) {
    perf_event_attr attr{};
    attr.size = sizeof(attr);
    attr.type = 12;
    attr.config = configs[i];
    attr.disabled = 1;
    attr.pinned = i == 0;
    attr.read_format = PERF_FORMAT_GROUP | PERF_FORMAT_ID |
        PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
    fds[i] = syscall(SYS_perf_event_open, &attr, -1, 0,
                     i == 0 ? -1 : fds[0], PERF_FLAG_FD_CLOEXEC);
    if (fds[i] < 0) return fail("open");
    if (ioctl(fds[i], PERF_EVENT_IOC_ID, &ids[i])) return fail("id");
  }
  if (ioctl(fds[0], PERF_EVENT_IOC_RESET, PERF_IOC_FLAG_GROUP))
    return fail("reset");
  if (ioctl(fds[0], PERF_EVENT_IOC_ENABLE, PERF_IOC_FLAG_GROUP))
    return fail("enable");
  timespec remaining{0, 200000000};
  while (nanosleep(&remaining, &remaining)) {
    if (errno != EINTR) return fail("sleep");
  }
  if (ioctl(fds[0], PERF_EVENT_IOC_DISABLE, PERF_IOC_FLAG_GROUP))
    return fail("disable");
  std::array<uint64_t, 11> data{};
  const ssize_t bytes = read(fds[0], data.data(), sizeof(data));
  if (bytes != sizeof(data) || data[0] != 4) return fail("read_length");
  std::printf("{\"enabled_ns\":%lu,\"running_ns\":%lu,\"events\":[",
              data[1], data[2]);
  bool valid = data[1] > 0 && data[1] == data[2];
  for (size_t i = 0; i < 4; ++i) {
    valid = valid && data[4 + 2 * i] == ids[i];
    std::printf("%s{\"config\":%lu,\"value\":%lu,\"id\":%lu}",
                i ? "," : "", configs[i], data[3 + 2 * i], data[4 + 2 * i]);
  }
  std::printf("],\"valid\":%s}\n", valid ? "true" : "false");
  for (int fd : fds) close(fd);
  return valid ? 0 : 1;
}
