#include "q4t/server/responses_owner.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <climits>
#include <stdexcept>

#include "object_protocol.h"
#include "q4t/service/guardian_process.h"

namespace q4t::server {
namespace {
std::string BootId() {
  service::OwnedFd fd(open("/proc/sys/kernel/random/boot_id",
                           O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
  if (fd.Get() < 0) return {};
  std::array<char, 64> bytes{};
  ssize_t size;
  do {
    size = read(fd.Get(), bytes.data(), bytes.size());
  } while (size < 0 && errno == EINTR);
  if (size != 37 || bytes[36] != '\n') return {};
  const std::string value(bytes.data(), 36);
  for (size_t i = 0; i < value.size(); ++i) {
    const bool dash = i == 8 || i == 13 || i == 18 || i == 23;
    if (dash ? value[i] != '-'
             : !((value[i] >= '0' && value[i] <= '9') ||
                 (value[i] >= 'a' && value[i] <= 'f')))
      return {};
  }
  return value;
}
uint64_t PidNamespace() {
  struct stat info {};
  return stat("/proc/self/ns/pid", &info) == 0 ? info.st_ino : 0;
}
}  // namespace

io::Json CurrentResponseOwner() {
  service::ProcessRecord process;
  const auto boot = BootId();
  const auto space = PidNamespace();
  if (boot.empty() || !space || !service::ReadProcess(getpid(), &process))
    throw std::runtime_error("Response execution identity unavailable");
  using namespace object_detail;
  return Object({{"pid", Number(process.identity.pid)},
                 {"start_ticks", Number(process.identity.start_ticks)},
                 {"boot_id", String(boot)},
                 {"pid_namespace", Number(space)}});
}

ResponseOwnerState ResponseOwnerStatus(const io::Json& owner) {
  const auto pid = owner.GetInt("pid"), ticks = owner.GetInt("start_ticks");
  const auto boot = BootId();
  const auto space = PidNamespace();
  if (pid <= 0 || pid > INT_MAX || ticks <= 0 ||
      owner.GetString("boot_id").size() != 36 ||
      owner.GetInt("pid_namespace") <= 0 || boot.empty() || !space)
    return ResponseOwnerState::kUnknown;
  if (boot != owner.GetString("boot_id")) return ResponseOwnerState::kGone;
  // The same boot may host other PID namespaces. Do not infer death from a
  // PID lookup in the wrong namespace after a deployment migration.
  if (space != static_cast<uint64_t>(owner.GetInt("pid_namespace")))
    return ResponseOwnerState::kUnknown;
  service::ProcessRecord current;
  bool gone = false;
  const auto status =
      service::ReadProcess(static_cast<pid_t>(pid), &current, &gone);
  if (gone) return ResponseOwnerState::kGone;
  if (!status) return ResponseOwnerState::kUnknown;
  if (current.identity.start_ticks != static_cast<uint64_t>(ticks) ||
      current.state == 'Z' || current.state == 'X')
    return ResponseOwnerState::kGone;
  return ResponseOwnerState::kAlive;
}
}  // namespace q4t::server
