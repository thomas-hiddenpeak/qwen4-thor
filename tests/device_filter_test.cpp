// Real, CPU-only Linux enforcement checks. Every rule is attached solely to a
// newly created test cgroup; no host-root attachment or NVIDIA access occurs.
#include "q4t/service/device_filter.h"
#include "q4t/service/device_admission.h"

#include <fcntl.h>
#include <grp.h>
#include <linux/bpf.h>
#include <linux/memfd.h>
#include <poll.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/file.h>
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

struct AdmissionFixture {
  Groups groups;
  s::DeviceFilter filter;
  fs::path directory = fs::absolute(fs::path(".q4t-work") /
      ("device-admission-cpu-" + std::to_string(getpid()) + "-" +
       std::to_string(++serial)));
  fs::path path = directory / "lease";
  s::OwnedFd lease, receipt, token;
  s::DeviceAdmission claims;
  uid_t uid = 0;
  gid_t gid = 0;

  AdmissionFixture() {
    // Use the actual unprivileged caller so its existing workspace parents
    // remain traversable; never change permissions on the user's home.
    const char* caller = getenv("SUDO_UID");
    const char* group = getenv("SUDO_GID");
    if (!caller || !group || std::stoul(caller) == 0 ||
        std::stoul(caller) >= UINT32_MAX || std::stoul(group) >= UINT32_MAX)
      throw std::runtime_error("CPU admission test requires sudo caller IDs");
    uid = static_cast<uid_t>(std::stoul(caller));
    gid = static_cast<gid_t>(std::stoul(group));
    Attach(&filter, groups);
    if (mkdir(directory.c_str(), 0755) != 0)
      throw std::runtime_error("create own CPU lease directory failed");
    created.push_back(directory);
    lease.Reset(open(path.c_str(), O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0600));
    struct stat file {};
    if (lease.Get() < 0 || fchown(lease.Get(), uid, gid) != 0 ||
        flock(lease.Get(), LOCK_EX | LOCK_NB) != 0 ||
        fstat(lease.Get(), &file) != 0)
      throw std::runtime_error("create held CPU lease failed");
    claims.service_id = "11111111-1111-4111-8111-111111111111";
    claims.domain_id = "22222222-2222-4222-8222-222222222222";
    claims.boot_id = "33333333-3333-4333-8333-333333333333";
    claims.manifest_sha256 = std::string(64, 'a');
    claims.qualification_sha256 = std::string(64, 'b');
    claims.journal_receipt_sha256 = std::string(64, 'c');
    claims.ticket_revision = (1ULL << 53) + 1;  // Exact JSON integer identity.
    claims.issued_ns = 1000000000;
    claims.expires_ns = 2000000000;
    claims.runtime_uid = uid;
    claims.lease_device = file.st_dev;
    claims.lease_inode = file.st_ino;
    claims.filter = filter.Identity();
  }
  ~AdmissionFixture() {
    lease.Reset();
    unlink(path.c_str());
    rmdir(directory.c_str());
  }
  void Issue() {
    Need(s::IssueDeviceAdmission(claims, filter, lease.Get(), path.string(),
                                &receipt, &token));
  }
  bool Drop() const {
    return setgroups(0, nullptr) == 0 && setgid(gid) == 0 && setuid(uid) == 0;
  }
  q4t::Status Consume(const s::DeviceAdmission& expected, uint64_t now) const {
    return s::ConsumeDeviceAdmission(receipt.Get(), token.Get(), expected,
        filter, lease.Get(), path.string(), now);
  }
  bool Consume() const { return Consume(claims, 1500000000).ok(); }
};
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

Q4T_TEST(device_admission_once_across_processes_and_inherited_filter) {
  AdmissionFixture f;
  f.Issue();
  s::DeviceAdmission inspected;
  Need(s::InspectDeviceAdmission(f.receipt.Get(), f.token.Get(), &inspected));
  Q4T_CHECK(inspected == f.claims);
  Q4T_CHECK(Child(f.groups.allowed, [&] {
    if (!f.Drop()) return false;
    s::DeviceFilter adopted;
    Need(adopted.AdoptInherited(f.filter.LinkFd(), f.filter.ProgramFd(),
                                inspected.filter));
    Need(adopted.Verify());
    Need(s::ConsumeDeviceAdmission(f.receipt.Get(), f.token.Get(), inspected,
        adopted, f.lease.Get(), f.path.string(), 1500000000));
    return !f.Consume();
  }));
  // fork/dup never regenerates a spent pipe token, even in another process.
  Q4T_CHECK(Child(f.groups.allowed, [&] {
    if (!f.Drop()) return false;
    s::OwnedFd copy(fcntl(f.token.Get(), F_DUPFD_CLOEXEC, 3));
    return !s::ConsumeDeviceAdmission(f.receipt.Get(), copy.Get(), f.claims,
        f.filter, f.lease.Get(), f.path.string(), 1500000000).ok();
  }));
  return true;
}

Q4T_TEST(device_admission_bad_binding_and_expiry_do_not_spend) {
  AdmissionFixture f;
  f.Issue();
  Q4T_CHECK(Child(f.groups.allowed, [&] {
    if (!f.Drop()) return false;
    for (int index = 0; index < 10; ++index) {
      auto bad = f.claims;
      if (index == 0) bad.service_id[0] = 'a';
      if (index == 1) bad.boot_id[0] = 'a';
      if (index == 2) bad.domain_id[0] = 'a';
      if (index == 3) bad.manifest_sha256[0] = '0';
      if (index == 4) ++bad.ticket_revision;
      if (index == 5) ++bad.runtime_uid;
      if (index == 6) ++bad.lease_inode;
      if (index == 7) ++bad.filter.allowed_cgroup_id;
      if (index == 8) bad.qualification_sha256[0] = '0';
      if (index == 9) bad.journal_receipt_sha256[0] = '0';
      if (f.Consume(bad, 1500000000).ok()) return false;
    }
    if (f.Consume(f.claims, f.claims.issued_ns - 1).ok() ||
        f.Consume(f.claims, f.claims.expires_ns).ok()) return false;
    return f.Consume();
  }));
  return true;
}

Q4T_TEST(device_admission_wrong_group_and_privilege_do_not_spend) {
  AdmissionFixture f;
  f.Issue();
  Q4T_CHECK(Child(f.groups.allowed, [&] { return !f.Consume(); }));
  for (const auto& group : {f.groups.denied, f.groups.nested})
    Q4T_CHECK(Child(group, [&] { return f.Drop() && !f.Consume(); }));
  Q4T_CHECK(Child(f.groups.allowed, [&] { return f.Drop() && f.Consume(); }));
  return true;
}

Q4T_TEST(device_admission_copied_receipt_or_unprivileged_token_is_not_authority) {
  AdmissionFixture f;
  f.Issue();
  Q4T_CHECK(Child(f.groups.allowed, [&] {
    if (!f.Drop()) return false;
    char bytes[8192];
    const auto length = pread(f.receipt.Get(), bytes, sizeof(bytes), 0);
    if (length <= 0) return false;
    s::OwnedFd copy(static_cast<int>(syscall(SYS_memfd_create, "copied-receipt",
                                            MFD_CLOEXEC | MFD_ALLOW_SEALING)));
    if (copy.Get() < 0 || write(copy.Get(), bytes, length) != length ||
        fcntl(copy.Get(), F_ADD_SEALS, F_SEAL_SEAL | F_SEAL_SHRINK |
                                     F_SEAL_GROW | F_SEAL_WRITE) != 0)
      return false;
    s::OwnedFd readonly(open(("/proc/self/fd/" + std::to_string(copy.Get())).c_str(),
                            O_RDONLY | O_CLOEXEC));
    s::DeviceAdmission result;
    if (s::InspectDeviceAdmission(readonly.Get(), f.token.Get(), &result).ok())
      return false;
    int pipes[2];
    if (pipe2(pipes, O_NONBLOCK | O_CLOEXEC) != 0) return false;
    s::OwnedFd own(pipes[0]), writer(pipes[1]);
    if (s::InspectDeviceAdmission(f.receipt.Get(), own.Get(), &result).ok())
      return false;
    if (fchmod(f.token.Get(), 0666) == 0 || errno != EPERM) return false;
    s::OwnedFd refill(open(("/proc/self/fd/" +
                           std::to_string(f.token.Get())).c_str(),
                          O_WRONLY | O_NONBLOCK | O_CLOEXEC));
    if (refill.Get() >= 0 || errno != EACCES) return false;
    s::OwnedFd issued, token;
    if (s::IssueDeviceAdmission(f.claims, f.filter, f.lease.Get(),
        f.path.string(), &issued, &token).ok()) return false;
    return f.Consume();
  }));
  return true;
}

Q4T_TEST(device_admission_requires_actual_continuous_lease) {
  AdmissionFixture f;
  f.Issue();
  Q4T_CHECK(Child(f.groups.allowed, [&] {
    if (!f.Drop()) return false;
    s::OwnedFd independent(open(f.path.c_str(), O_RDWR | O_CLOEXEC));
    if (s::ConsumeDeviceAdmission(f.receipt.Get(), f.token.Get(), f.claims,
        f.filter, independent.Get(), f.path.string(), 1500000000).ok())
      return false;
    return f.Consume();
  }));
  f.receipt.Reset();
  f.token.Reset();
  f.Issue();
  Q4T_CHECK(flock(f.lease.Get(), LOCK_UN) == 0);
  Q4T_CHECK(Child(f.groups.allowed, [&] { return f.Drop() && !f.Consume(); }));
  s::OwnedFd issued, token;
  Q4T_CHECK(!s::IssueDeviceAdmission(f.claims, f.filter, f.lease.Get(),
      f.path.string(), &issued, &token).ok());
  Q4T_CHECK(issued.Get() < 0 && token.Get() < 0);
  return true;
}

Q4T_TEST(device_admission_rejects_replaced_kernel_policy) {
  AdmissionFixture f;
  f.Issue();
  s::DeviceFilter other;
  Attach(&other, f.groups, {{1, 3}});
  s::DeviceFilter wrong;
  Q4T_CHECK(!wrong.AdoptInherited(other.LinkFd(), other.ProgramFd(),
                                   f.claims.filter).ok());
  Q4T_CHECK(wrong.LinkFd() < 0 && wrong.ProgramFd() < 0);
  bpf_attr attribute{};
  attribute.link_update.link_fd = f.filter.LinkFd();
  attribute.link_update.new_prog_fd = other.ProgramFd();
  Q4T_CHECK(syscall(SYS_bpf, BPF_LINK_UPDATE, &attribute, sizeof(attribute)) == 0);
  Q4T_CHECK(Child(f.groups.allowed, [&] { return f.Drop() && !f.Consume(); }));
  pollfd still_unused{f.token.Get(), POLLIN, 0};
  Q4T_CHECK(poll(&still_unused, 1, 0) == 1 &&
            (still_unused.revents & POLLIN));
  return true;
}

Q4T_TEST(device_admission_rejects_invalid_issuance_without_outputs) {
  AdmissionFixture f;
  for (int index = 0; index < 7; ++index) {
    auto bad = f.claims;
    if (index == 0) bad.expires_ns = bad.issued_ns;
    if (index == 1) bad.expires_ns = bad.issued_ns + 300000000001ULL;
    if (index == 2) bad.runtime_uid = 0;
    if (index == 3) bad.manifest_sha256[0] = 'z';
    if (index == 4) bad.filter.program_id++;
    if (index == 5) bad.lease_inode++;
    if (index == 6) bad.ticket_revision = 0;
    s::OwnedFd receipt, token;
    Q4T_CHECK(!s::IssueDeviceAdmission(bad, f.filter, f.lease.Get(),
        f.path.string(), &receipt, &token).ok());
    Q4T_CHECK(receipt.Get() < 0 && token.Get() < 0);
  }
  f.Issue();
  const int receipt = f.receipt.Get(), token = f.token.Get();
  Q4T_CHECK(!s::IssueDeviceAdmission(f.claims, f.filter, f.lease.Get(),
      f.path.string(), &f.receipt, &f.token).ok());
  Q4T_CHECK(f.receipt.Get() == receipt && f.token.Get() == token);
  Q4T_CHECK(Child(f.groups.allowed, [&] { return f.Drop() && f.Consume(); }));
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
