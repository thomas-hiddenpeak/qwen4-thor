// Real, CPU-only Linux enforcement checks. Every rule is attached solely to a
// newly created test cgroup; no host-root attachment or NVIDIA access occurs.
#include "q4t/service/device_filter.h"

#include <fcntl.h>
#include <grp.h>
#include <linux/bpf.h>
#include <poll.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <filesystem>
#include <functional>
#include <sstream>
#include <stdexcept>
#include <string>

#include "q4t/test.h"

namespace {
namespace s = q4t::service;
namespace fs = std::filesystem;
unsigned serial = 0;
std::vector<fs::path> created;

void Need(const q4t::Status& status) {
  if (!status.ok()) throw std::runtime_error(status.message());
}

struct Groups {
  fs::path base = fs::path("/sys/fs/cgroup") /
                  ("q4t-device-test-" + std::to_string(getpid()) + "-" +
                   std::to_string(++serial));
  fs::path allowed = base / "allowed";
  fs::path denied = base / "denied";
  fs::path nested = allowed / "nested";
  s::OwnedFd target_fd;
  s::OwnedFd allowed_fd;
  Groups() {
    for (const auto& path : {base, allowed, denied, nested}) {
      if (mkdir(path.c_str(), 0755) != 0)
        throw std::runtime_error("create owned test cgroup failed");
      created.push_back(path);
    }
    target_fd.Reset(open(base.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC));
    allowed_fd.Reset(open(allowed.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC));
    if (target_fd.Get() < 0 || allowed_fd.Get() < 0)
      throw std::runtime_error("open test cgroup failed");
  }
  ~Groups() {
    // No recursive deletion; an unexpected member leaves the owned path for
    // inspection, and the final cleanup audit makes the test fail.
    for (const auto& path : {nested, denied, allowed, base}) rmdir(path.c_str());
  }
};

struct Pins {
  fs::path directory = fs::path("/sys/fs/bpf") /
      ("q4t-device-test-" + std::to_string(getpid()) + "-" +
       std::to_string(++serial));
  fs::path policy = directory / "policy";
  fs::path other = directory / "other";
  Pins() {
    if (mkdir(directory.c_str(), 0700) != 0)
      throw std::runtime_error("create owned CPU-test bpffs directory failed");
    created.push_back(directory);
  }
  ~Pins() {
    // These two fixed entries are only created by the current test process.
    unlink(policy.c_str());
    unlink(other.c_str());
    rmdir(directory.c_str());
  }
};

bool Join(const fs::path& group) {
  s::OwnedFd fd(open((group / "cgroup.procs").c_str(), O_WRONLY | O_CLOEXEC));
  const auto pid = std::to_string(getpid());
  return fd.Get() >= 0 &&
         write(fd.Get(), pid.data(), pid.size()) ==
             static_cast<ssize_t>(pid.size());
}

bool Child(const fs::path& group, const std::function<bool()>& body) {
  const pid_t pid = fork();
  if (pid < 0) return false;
  if (pid == 0) {
    int code = 1;
    try {
      if (Join(group) && body()) code = 0;
    } catch (const std::exception& error) {
      dprintf(STDERR_FILENO, "CPU fixture: %s\n", error.what());
    }
    _exit(code);
  }
  s::OwnedFd pidfd(static_cast<int>(syscall(SYS_pidfd_open, pid, 0)));
  pollfd wait{pidfd.Get(), POLLIN, 0};
  const int result = pidfd.Get() < 0 ? -1 : poll(&wait, 1, 5000);
  if (result != 1) kill(pid, SIGKILL);  // Unreaped direct child, no PID reuse.
  int status = 0;
  while (waitpid(pid, &status, 0) < 0)
    if (errno != EINTR) return false;
  return result == 1 && WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

bool Zero(bool allow) {
  s::OwnedFd fd(open("/dev/zero", O_RDONLY | O_CLOEXEC));
  if (!allow) return fd.Get() < 0 && errno == EPERM;
  char byte = 1;
  return fd.Get() >= 0 && read(fd.Get(), &byte, 1) == 1 && byte == 0;
}

void Attach(s::DeviceFilter* filter, const Groups& groups,
            std::vector<s::DeviceNumber> devices = {{1, 5}}) {
  Need(filter->Attach(groups.target_fd.Get(), groups.allowed_fd.Get(), devices));
  Need(filter->Verify());
}
}  // namespace

Q4T_TEST(device_filter_rejects_invalid_scope_before_attachment) {
  s::OwnedFd ordinary(open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC));
  uint64_t id = 0;
  Q4T_CHECK(!s::ReadCgroupId(ordinary.Get(), &id).ok());
  Q4T_CHECK(!s::ReadCgroupId(-1, &id).ok());
  Groups first;
  Groups second;
  s::DeviceFilter filter;
  Q4T_CHECK(!filter.Verify().ok());
  Q4T_CHECK(!filter.Attach(first.target_fd.Get(), first.target_fd.Get(),
                           {{1, 5}}).ok());
  Q4T_CHECK(!filter.Attach(first.target_fd.Get(), second.allowed_fd.Get(),
                           {{1, 5}}).ok());
  Q4T_CHECK(!filter.Attach(first.target_fd.Get(), first.allowed_fd.Get(),
                           {}).ok());
  Q4T_CHECK(!filter.Attach(first.target_fd.Get(), first.allowed_fd.Get(),
                           {{1, 5}, {1, 5}}).ok());
  Q4T_CHECK(!filter.Attach(first.target_fd.Get(), first.allowed_fd.Get(),
                           {{4096, 0}}).ok());
  Q4T_CHECK(!filter.Attach(first.target_fd.Get(), first.allowed_fd.Get(),
                           {{1, 1U << 20}}).ok());
  Q4T_CHECK(filter.LinkFd() < 0 && filter.ProgramFd() < 0);
  Q4T_CHECK(Child(first.denied, [] { return Zero(true); }));
  return true;
}

Q4T_TEST(device_filter_enforces_exact_cgroup_in_real_kernel) {
  Groups groups;
  s::DeviceFilter filter;
  Attach(&filter, groups);
  Q4T_CHECK(Child(groups.allowed, [] { return Zero(true); }));
  Q4T_CHECK(Child(groups.denied, [] { return Zero(false); }));
  Q4T_CHECK(Child(groups.base, [] { return Zero(false); }));
  Q4T_CHECK(Child(groups.nested, [] { return Zero(false); }));
  Q4T_CHECK(Child(groups.denied, [] {
    s::OwnedFd other(open("/dev/null", O_WRONLY | O_CLOEXEC));
    return other.Get() >= 0 && write(other.Get(), "x", 1) == 1;
  }));
  Q4T_CHECK(Zero(true));  // Parent never joined the test domain.
  Q4T_CHECK(!filter.Attach(groups.target_fd.Get(), groups.allowed_fd.Get(),
                           {{1, 3}}).ok());
  Q4T_CHECK(filter.Verify().ok());
  return true;
}

Q4T_TEST(device_filter_custody_survives_creator_handle_close) {
  Groups groups;
  s::OwnedFd custody;
  {
    s::DeviceFilter filter;
    Attach(&filter, groups);
    custody.Reset(fcntl(filter.LinkFd(), F_DUPFD_CLOEXEC, 3));
    Q4T_CHECK(custody.Get() >= 0);
  }
  Q4T_CHECK(Child(groups.denied, [] { return Zero(false); }));
  Q4T_CHECK(Child(groups.allowed, [] { return Zero(true); }));
  custody.Reset();
  Q4T_CHECK(Child(groups.denied, [] { return Zero(true); }));
  return true;
}

Q4T_TEST(device_filter_existing_handle_is_not_revoked_or_proof_of_idle) {
  Groups groups;
  s::OwnedFd existing(open("/dev/zero", O_RDONLY | O_CLOEXEC));
  Q4T_CHECK(existing.Get() >= 0);
  s::DeviceFilter filter;
  Attach(&filter, groups);
  Q4T_CHECK(Child(groups.denied, [&] {
    char byte = 1;
    return Zero(false) && read(existing.Get(), &byte, 1) == 1 && byte == 0;
  }));
  return true;
}

Q4T_TEST(device_filter_does_not_detach_other_policy_and_detects_detach) {
  Groups groups;
  s::DeviceFilter retained;
  Attach(&retained, groups, {{1, 3}});
  {
    s::DeviceFilter removed;
    Attach(&removed, groups);
    Q4T_CHECK(Child(groups.denied, [] { return Zero(false); }));
    bpf_attr attribute{};
    attribute.link_detach.link_fd = removed.LinkFd();
    Q4T_CHECK(syscall(SYS_bpf, BPF_LINK_DETACH, &attribute,
                       sizeof(attribute)) == 0);
    Q4T_CHECK(!removed.Verify().ok());
  }
  Q4T_CHECK(retained.Verify().ok());
  Q4T_CHECK(Child(groups.denied, [] {
    s::OwnedFd blocked(open("/dev/null", O_WRONLY | O_CLOEXEC));
    return blocked.Get() < 0 && errno == EPERM && Zero(true);
  }));
  return true;
}

Q4T_TEST(device_filter_unprivileged_client_cannot_join_allowed_group) {
  Groups groups;
  s::DeviceFilter filter;
  Attach(&filter, groups);
  Q4T_CHECK(Child(groups.denied, [&] {
    if (setgroups(0, nullptr) != 0 || setgid(65534) != 0 ||
        setuid(65534) != 0) return false;
    return !Join(groups.allowed) && !Join(groups.base) && Zero(false);
  }));
  return true;
}

Q4T_TEST(device_filter_pin_survives_real_creator_exit_and_reopens) {
  Groups groups;
  Pins pins;
  int pipe[2];
  Q4T_CHECK(pipe2(pipe, O_CLOEXEC) == 0);
  s::OwnedFd input(pipe[0]), output(pipe[1]);
  Q4T_CHECK(Child(groups.allowed, [&] {
    s::DeviceFilter creator;
    Attach(&creator, groups);
    Need(creator.Pin(pins.policy.string()));
    const auto identity = creator.Identity();
    std::ostringstream serialized;
    serialized << identity.program_id << ' ' << identity.link_id << ' '
               << identity.target_cgroup_id << ' ' << identity.allowed_cgroup_id
               << ' ' << identity.program_tag;
    const auto bytes = serialized.str();
    return write(output.Get(), bytes.data(), bytes.size()) ==
           static_cast<ssize_t>(bytes.size());
  }));
  output.Reset();
  char bytes[256]{};
  const auto size = read(input.Get(), bytes, sizeof(bytes) - 1);
  Q4T_CHECK(size > 0);
  s::DeviceFilterIdentity identity;
  std::istringstream serialized(std::string(bytes, size));
  Q4T_CHECK(static_cast<bool>(serialized >> identity.program_id >> identity.link_id
      >> identity.target_cgroup_id >> identity.allowed_cgroup_id
      >> identity.program_tag));
  Q4T_CHECK(Child(groups.denied, [] { return Zero(false); }));
  {
    s::DeviceFilter successor;
    Need(successor.AdoptPinned(pins.policy.string(), groups.target_fd.Get(),
                               groups.allowed_fd.Get(), {{1, 5}}, identity));
    Q4T_CHECK(successor.Identity() == identity);
    Q4T_CHECK(Child(groups.allowed, [&] {
      if (setgroups(0, nullptr) != 0 || setgid(65534) != 0 ||
          setuid(65534) != 0) return false;
      // Privileged deployment does not make the running Guardian privileged.
      return successor.Verify().ok() && Zero(true);
    }));
    Need(successor.RemovePin(pins.policy.string()));
    Q4T_CHECK(!fs::exists(pins.policy));
    Q4T_CHECK(Child(groups.denied, [] { return Zero(false); }));
  }
  Q4T_CHECK(Child(groups.denied, [] { return Zero(true); }));
  return true;
}

Q4T_TEST(device_filter_adoption_verifies_code_and_retained_identity) {
  Groups groups;
  Pins pins;
  s::DeviceFilter original;
  Attach(&original, groups);
  Need(original.Pin(pins.policy.string()));
  const auto expected = original.Identity();
  for (int mutation = 0; mutation < 6; ++mutation) {
    auto identity = expected;
    std::vector<s::DeviceNumber> rules{{1, 5}};
    if (mutation == 0) ++identity.program_id;
    if (mutation == 1) ++identity.link_id;
    if (mutation == 2) ++identity.target_cgroup_id;
    if (mutation == 3) ++identity.allowed_cgroup_id;
    if (mutation == 4) identity.program_tag = std::string(16, '0');
    if (mutation == 5) rules = {{1, 3}};
    s::DeviceFilter refused;
    Q4T_CHECK(!refused.AdoptPinned(pins.policy.string(), groups.target_fd.Get(),
        groups.allowed_fd.Get(), rules, identity).ok());
    Q4T_CHECK(refused.LinkFd() < 0 && refused.ProgramFd() < 0);
  }
  Need(original.Verify());
  Q4T_CHECK(Child(groups.denied, [] { return Zero(false); }));
  Need(original.RemovePin(pins.policy.string()));
  return true;
}

Q4T_TEST(device_filter_pin_paths_and_other_registration_are_protected) {
  Groups groups;
  Pins pins;
  s::DeviceFilter original, other;
  Attach(&original, groups);
  Attach(&other, groups, {{1, 3}});
  Q4T_CHECK(!original.Pin((groups.base / "policy").string()).ok());
  Q4T_CHECK(!original.Pin((pins.directory / "..").string()).ok());
  Q4T_CHECK(chmod(pins.directory.c_str(), 0777) == 0);
  Q4T_CHECK(!original.Pin(pins.policy.string()).ok());
  Q4T_CHECK(chmod(pins.directory.c_str(), 0700) == 0);
  Need(original.Pin(pins.policy.string()));
  Q4T_CHECK(!other.Pin(pins.policy.string()).ok());
  Need(other.Pin(pins.other.string()));
  Q4T_CHECK(!original.RemovePin(pins.other.string()).ok());
  Q4T_CHECK(fs::exists(pins.other));
  Need(original.RemovePin(pins.policy.string()));
  s::DeviceFilter missing;
  Q4T_CHECK(!missing.AdoptPinned(pins.policy.string(), groups.target_fd.Get(),
      groups.allowed_fd.Get(), {{1, 5}}, original.Identity()).ok());
  Need(other.Verify());
  Need(other.RemovePin(pins.other.string()));
  return true;
}

Q4T_TEST(device_filter_kernel_program_replacement_invalidates_old_receipt) {
  Groups groups;
  Pins pins;
  s::DeviceFilter original, replacement;
  Attach(&original, groups);
  Attach(&replacement, groups, {{1, 3}});
  Need(original.Pin(pins.policy.string()));
  const auto expected = original.Identity();
  bpf_attr attribute{};
  attribute.link_update.link_fd = original.LinkFd();
  attribute.link_update.new_prog_fd = replacement.ProgramFd();
  Q4T_CHECK(syscall(SYS_bpf, BPF_LINK_UPDATE, &attribute,
                     sizeof(attribute)) == 0);
  Q4T_CHECK(!original.Verify().ok());
  Q4T_CHECK(!original.RemovePin(pins.policy.string()).ok());
  s::DeviceFilter refused;
  Q4T_CHECK(!refused.AdoptPinned(pins.policy.string(), groups.target_fd.Get(),
      groups.allowed_fd.Get(), {{1, 5}}, expected).ok());
  Q4T_CHECK(Child(groups.denied, [] { return Zero(true); }));
  Need(replacement.Verify());
  return true;
}

Q4T_TEST(device_filter_covers_whole_character_major_without_alias_gaps) {
  Groups groups;
  Pins pins;
  s::DeviceFilter original;
  const std::vector<s::DeviceNumber> rules{{1, 0, true}};
  Attach(&original, groups, rules);
  Need(original.Pin(pins.policy.string()));
  Q4T_CHECK(Child(groups.denied, [] {
    if (!Zero(false)) return false;
    s::OwnedFd other(open("/dev/null", O_WRONLY | O_CLOEXEC));
    return other.Get() < 0 && errno == EPERM;
  }));
  Q4T_CHECK(Child(groups.allowed, [] {
    if (!Zero(true)) return false;
    s::OwnedFd other(open("/dev/null", O_WRONLY | O_CLOEXEC));
    return other.Get() >= 0;
  }));
  s::DeviceFilter successor;
  Need(successor.AdoptPinned(pins.policy.string(), groups.target_fd.Get(),
      groups.allowed_fd.Get(), rules, original.Identity()));
  for (const std::vector<s::DeviceNumber>& ambiguous : {
       std::vector<s::DeviceNumber>{{1, 5, true}},
       std::vector<s::DeviceNumber>{{1, 0, true}, {1, 5}},
       std::vector<s::DeviceNumber>{{1, 5}, {1, 0, true}}}) {
    s::DeviceFilter refused;
    Q4T_CHECK(!refused.Attach(groups.target_fd.Get(), groups.allowed_fd.Get(),
                              ambiguous).ok());
    Q4T_CHECK(refused.LinkFd() < 0);
  }
  Need(original.RemovePin(pins.policy.string()));
  return true;
}

int main(int argc, char** argv) {
  if (geteuid() != 0) {
    std::fputs("This CPU test requires explicit root cgroup privileges\n",
               stderr);
    return 2;
  }
  std::vector<std::string> names;
  if (argc == 1) {
    for (const auto& test : q4t::test::Registry()) names.push_back(test.name);
  } else {
    for (int index = 1; index < argc; ++index) names.emplace_back(argv[index]);
  }
  const int result = q4t::test::RunTests(names, false);
  for (const auto& path : created) {
    if (fs::exists(path)) {
      std::fprintf(stderr, "Owned cgroup cleanup incomplete: %s\n",
                   path.c_str());
      return 1;
    }
  }
  return result;
}
