// External, single-window UCF observer. No CUDA calls or server injection.
// Build separately under .q4t-work; never linked into q4t.
#include <linux/perf_event.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <string>

namespace {
constexpr std::array<uint64_t, 4> kConfigs{0x100000000ULL, 0, 6, 12};
constexpr const char* kPmu =
    "/sys/bus/event_source/devices/nvidia_ucf_gpu_pmu_0/";
uint64_t Now() {
  timespec ts{};
  if (clock_gettime(CLOCK_MONOTONIC, &ts)) std::abort();
  return static_cast<uint64_t>(ts.tv_sec) * 1000000000ULL + ts.tv_nsec;
}
class Group {
 public:
  ~Group() {
    for (int fd : fds_) if (fd >= 0) close(fd);
  }
  bool Open() {
    unsigned type = 0;
    std::ifstream file(std::string(kPmu) + "type");
    if (!(file >> type)) return Error("pmu_type");
    // CPU0 is verified against the PMU's actual attachment mask.
    std::ifstream mask(std::string(kPmu) + "cpumask");
    std::string cpus;
    if (!(mask >> cpus) || cpus != "0") return Error("cpu_mask");
    for (size_t i = 0; i < kConfigs.size(); ++i) {
      perf_event_attr attr{};
      attr.size = sizeof(attr);
      attr.type = type;
      attr.config = kConfigs[i];
      attr.disabled = 1;
      attr.pinned = i == 0;
      attr.read_format = PERF_FORMAT_GROUP | PERF_FORMAT_ID |
          PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
      fds_[i] = syscall(SYS_perf_event_open, &attr, -1, 0,
                       i == 0 ? -1 : fds_[0], PERF_FLAG_FD_CLOEXEC);
      if (fds_[i] < 0) return Error("open");
      if (ioctl(fds_[i], PERF_EVENT_IOC_ID, &ids_[i])) return Error("id");
    }
    std::printf("{\"event\":\"ready\",\"type\":%u,\"cpu\":0}\n", type);
    return true;
  }
  bool Control(unsigned long request, const char* event) {
    const uint64_t begin = Now();
    if (ioctl(fds_[0], request, PERF_IOC_FLAG_GROUP)) return Error(event);
    std::printf("{\"event\":\"%s\",\"begin_ns\":%lu,\"end_ns\":%lu}\n",
                event, begin, Now());
    return true;
  }
  bool Snapshot() {
    std::array<uint64_t, 11> data{};
    const uint64_t begin = Now();
    const ssize_t bytes = read(fds_[0], data.data(), sizeof(data));
    const uint64_t end = Now();
    if (bytes != sizeof(data) || data[0] != 4) return Error("read_size");
    bool valid = data[1] > 0 && data[1] == data[2];
    std::printf("{\"event\":\"snapshot\",\"begin_ns\":%lu,\"end_ns\":%lu,"
                "\"enabled_ns\":%lu,\"running_ns\":%lu,\"events\":[",
                begin, end, data[1], data[2]);
    for (size_t i = 0; i < kConfigs.size(); ++i) {
      valid = valid && data[4 + 2 * i] == ids_[i];
      std::printf("%s{\"config\":%lu,\"value\":%lu,\"id\":%lu}",
                  i ? "," : "", kConfigs[i], data[3 + 2 * i],
                  data[4 + 2 * i]);
    }
    std::printf("],\"valid\":%s}\n", valid ? "true" : "false");
    return valid;
  }
  static bool Error(const char* operation) {
    std::printf("{\"error\":\"%s\",\"errno\":%d}\n", operation, errno);
    return false;
  }
 private:
  std::array<int, 4> fds_{-1, -1, -1, -1};
  std::array<uint64_t, 4> ids_{};
};
}  // namespace

int main() {
  setvbuf(stdout, nullptr, _IOLBF, 0);
  Group group;
  if (!group.Open()) return 1;
  std::string command;
  if (!std::getline(std::cin, command) || command != "start") {
    Group::Error("expected_start");
    return 2;
  }
  if (!group.Control(PERF_EVENT_IOC_ENABLE, "started")) return 1;
  while (std::getline(std::cin, command)) {
    if (command == "read") {
      if (!group.Snapshot()) return 1;
    } else if (command == "stop") {
      if (!group.Control(PERF_EVENT_IOC_DISABLE, "stopped")) return 1;
      return group.Snapshot() ? 0 : 1;
    } else {
      Group::Error("expected_read_or_stop");
      return 2;
    }
  }
  Group::Error("unexpected_eof");
  return 2;
}
