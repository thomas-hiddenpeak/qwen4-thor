#include "q4t/service/device_admission.h"

#include <fcntl.h>
#include <linux/memfd.h>
#include <poll.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <sstream>
#include <stdexcept>

#include "q4t/service/guardian_wire.h"

namespace q4t::service {
namespace {
namespace w = wire;
using io::Json;
constexpr size_t kMaximum = 8192;
constexpr uint64_t kLifetime = 300ULL * 1000000000;
constexpr int kSeals = F_SEAL_SEAL | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE;

void Need(bool value, const char* reason) {
  if (!value) throw std::runtime_error(reason);
}
void Need(const Status& status) {
  if (!status.ok()) throw std::runtime_error(status.message());
}
bool Hex(const std::string& value, size_t size) {
  return value.size() == size &&
         value.find_first_not_of("0123456789abcdef") == std::string::npos;
}
bool Uuid(const std::string& value) {
  if (value.size() != 36) return false;
  for (size_t index = 0; index < value.size(); ++index) {
    if (index == 8 || index == 13 || index == 18 || index == 23) {
      if (value[index] != '-') return false;
    } else if (!((value[index] >= '0' && value[index] <= '9') ||
                 (value[index] >= 'a' && value[index] <= 'f'))) {
      return false;
    }
  }
  return true;
}
uint64_t Number(const Json& value, const char* name) {
  uint64_t result = 0;
  Need(w::GetUnsigned(value, name, &result));
  return result;
}
uint32_t Number32(const Json& value, const char* name) {
  const auto result = Number(value, name);
  Need(result <= UINT32_MAX, "device admission number exceeds bound");
  return static_cast<uint32_t>(result);
}
std::string Text(const Json& value, const char* name) {
  const auto* field = value.Find(name);
  Need(field && field->IsString() && field->str.size() <= 128,
       "invalid device admission string");
  return field->str;
}

void Validate(const DeviceAdmission& value) {
  Need(Uuid(value.service_id) && Uuid(value.domain_id) && Uuid(value.boot_id),
       "invalid device admission UUID");
  Need(Hex(value.manifest_sha256, 64) &&
           Hex(value.qualification_sha256, 64) &&
           Hex(value.journal_receipt_sha256, 64),
       "invalid device admission provenance");
  Need(value.ticket_revision > 0 && value.runtime_uid > 0 &&
           value.runtime_uid < UINT32_MAX && value.lease_inode > 0 &&
           value.lease_device > 0,
       "invalid device admission charge or runtime binding");
  Need(value.issued_ns > 0 && value.expires_ns > value.issued_ns &&
           value.expires_ns - value.issued_ns <= kLifetime,
       "invalid device admission lifetime");
  const auto& filter = value.filter;
  Need(filter.program_id > 0 && filter.link_id > 0 &&
           filter.target_cgroup_id > 0 && filter.allowed_cgroup_id > 0 &&
           filter.target_cgroup_id != filter.allowed_cgroup_id &&
           Hex(filter.program_tag, 16),
       "invalid device admission kernel binding");
}

Json Claims(const DeviceAdmission& value) {
  const auto& filter = value.filter;
  return w::Object({
      {"service_id", w::String(value.service_id)},
      {"manifest_sha256", w::String(value.manifest_sha256)},
      {"boot_id", w::String(value.boot_id)},
      {"domain_id", w::String(value.domain_id)},
      {"qualification_sha256", w::String(value.qualification_sha256)},
      {"journal_receipt_sha256", w::String(value.journal_receipt_sha256)},
      {"ticket_revision", w::Unsigned(value.ticket_revision)},
      {"issued_ns", w::Unsigned(value.issued_ns)},
      {"expires_ns", w::Unsigned(value.expires_ns)},
      {"runtime_uid", w::Unsigned(value.runtime_uid)},
      {"lease_device", w::Unsigned(value.lease_device)},
      {"lease_inode", w::Unsigned(value.lease_inode)},
      {"filter", w::Object({
          {"program_id", w::Unsigned(filter.program_id)},
          {"link_id", w::Unsigned(filter.link_id)},
          {"target_cgroup_id", w::Unsigned(filter.target_cgroup_id)},
          {"allowed_cgroup_id", w::Unsigned(filter.allowed_cgroup_id)},
          {"program_tag", w::String(filter.program_tag)}})}});
}

DeviceAdmission Decode(const Json& value) {
  Need(w::HasKeys(value, {"service_id", "manifest_sha256", "boot_id",
      "domain_id", "qualification_sha256", "journal_receipt_sha256",
      "ticket_revision", "issued_ns", "expires_ns", "runtime_uid",
      "lease_device", "lease_inode", "filter"}),
      "invalid device admission claims");
  DeviceAdmission result;
  result.service_id = Text(value, "service_id");
  result.manifest_sha256 = Text(value, "manifest_sha256");
  result.boot_id = Text(value, "boot_id");
  result.domain_id = Text(value, "domain_id");
  result.qualification_sha256 = Text(value, "qualification_sha256");
  result.journal_receipt_sha256 = Text(value, "journal_receipt_sha256");
  result.ticket_revision = Number(value, "ticket_revision");
  result.issued_ns = Number(value, "issued_ns");
  result.expires_ns = Number(value, "expires_ns");
  result.runtime_uid = Number32(value, "runtime_uid");
  result.lease_device = Number(value, "lease_device");
  result.lease_inode = Number(value, "lease_inode");
  const auto& filter = *value.Find("filter");
  Need(w::HasKeys(filter, {"program_id", "link_id", "target_cgroup_id",
                         "allowed_cgroup_id", "program_tag"}),
       "invalid device admission filter");
  result.filter.program_id = Number32(filter, "program_id");
  result.filter.link_id = Number32(filter, "link_id");
  result.filter.target_cgroup_id = Number(filter, "target_cgroup_id");
  result.filter.allowed_cgroup_id = Number(filter, "allowed_cgroup_id");
  result.filter.program_tag = Text(filter, "program_tag");
  Validate(result);
  return result;
}

struct Record {
  DeviceAdmission claims;
  std::string nonce;
};

Record Inspect(int receipt, int token) {
  struct stat file {}, pipe {};
  Need(receipt >= 3 && token >= 3 && receipt != token &&
           fstat(receipt, &file) == 0 && fstat(token, &pipe) == 0,
       "device admission descriptors unavailable");
  Need(S_ISREG(file.st_mode) && file.st_uid == 0 && file.st_nlink == 0 &&
           (file.st_mode & 0077) == 0 && file.st_size > 0 &&
           static_cast<uint64_t>(file.st_size) <= kMaximum &&
           fcntl(receipt, F_GET_SEALS) == kSeals &&
           (fcntl(receipt, F_GETFL) & O_ACCMODE) == O_RDONLY,
       "device admission is not an immutable root receipt");
  Need(S_ISFIFO(pipe.st_mode) && pipe.st_uid == 0 &&
           (pipe.st_mode & 0077) == 0 &&
           (fcntl(token, F_GETFL) & O_ACCMODE) == O_RDONLY &&
           (fcntl(token, F_GETFL) & O_NONBLOCK),
       "device admission is not a protected read-only token");
  std::string bytes(static_cast<size_t>(file.st_size), '\0');
  Need(pread(receipt, bytes.data(), bytes.size(), 0) == file.st_size,
       "device admission receipt read incomplete");
  Json value;
  Need(w::Parse(bytes, &value));
  Need(w::HasKeys(value, {"format", "claims", "pipe_device", "pipe_inode",
                         "nonce"}) &&
           Text(value, "format") == "q4t-device-admission-v1" &&
           Number(value, "pipe_device") == pipe.st_dev &&
           Number(value, "pipe_inode") == pipe.st_ino,
       "device admission token binding changed");
  Record result{Decode(*value.Find("claims")), Text(value, "nonce")};
  Need(Hex(result.nonce, 32), "invalid device admission nonce");
  return result;
}

uint64_t CurrentCgroup() {
  OwnedFd source(open("/proc/self/cgroup", O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
  Need(source.Get() >= 0, "device caller cgroup unavailable");
  std::array<char, 8193> bytes{};
  const auto length = read(source.Get(), bytes.data(), bytes.size());
  Need(length > 0 && length < 8193, "device caller cgroup exceeds bound");
  std::istringstream lines(std::string(bytes.data(), length));
  std::string line, group;
  while (std::getline(lines, line)) {
    if (!line.starts_with("0::/")) continue;
    Need(group.empty(), "repeated device cgroup identity");
    group = line.substr(3);
  }
  Need(!group.empty() && group.find("..") == std::string::npos &&
           group.find_first_of("\\\r\n\0", 0, 4) == std::string::npos,
       "invalid device cgroup path");
  OwnedFd directory(open(("/sys/fs/cgroup" + group).c_str(),
      O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
  uint64_t id = 0;
  Need(ReadCgroupId(directory.Get(), &id));
  return id;
}
}  // namespace

Status ParseDeviceAdmission(const std::string& bytes, DeviceAdmission* output) {
  try {
    Need(output && !bytes.empty() && bytes.size() <= kMaximum,
         "invalid device admission input size");
    Json value;
    Need(w::Parse(bytes, &value));
    *output = Decode(value);
    return {};
  } catch (const std::exception& error) {
    return Status::Fail(error.what());
  }
}

Status IssueDeviceAdmission(const DeviceAdmission& admission,
                            const DeviceFilter& filter, int lease_fd,
                            const std::string& lease_path,
                            OwnedFd* receipt, OwnedFd* token) {
  try {
    Need(geteuid() == 0 && receipt && token && receipt != token &&
             receipt->Get() < 0 && token->Get() < 0,
         "device admission requires explicit privileged issuance");
    Validate(admission);
    Need(filter.Verify());
    Need(filter.Identity() == admission.filter,
         "device admission filter differs from actual custody");
    Need(CheckInheritedLease(lease_fd, lease_path));
    struct stat lease {};
    Need(fstat(lease_fd, &lease) == 0 && S_ISREG(lease.st_mode) &&
             lease.st_uid == admission.runtime_uid && lease.st_nlink == 1 &&
             lease.st_dev == admission.lease_device &&
             lease.st_ino == admission.lease_inode,
         "device admission lease identity differs");
    int pipes[2];
    Need(pipe2(pipes, O_CLOEXEC | O_NONBLOCK) == 0,
         "create device admission token failed");
    OwnedFd input(pipes[0]), output(pipes[1]);
    Need(fchmod(input.Get(), 0600) == 0, "protect device token failed");
    struct stat pipe {};
    Need(fstat(input.Get(), &pipe) == 0, "inspect device token failed");
    std::array<unsigned char, 16> random{};
    Need(getrandom(random.data(), random.size(), GRND_NONBLOCK) ==
             static_cast<ssize_t>(random.size()),
         "device admission entropy unavailable");
    constexpr char hex[] = "0123456789abcdef";
    std::string nonce;
    for (unsigned char byte : random) {
      nonce.push_back(hex[byte >> 4]);
      nonce.push_back(hex[byte & 15]);
    }
    std::string bytes;
    Need(w::Encode(w::Object({
        {"format", w::String("q4t-device-admission-v1")},
        {"claims", Claims(admission)},
        {"pipe_device", w::Unsigned(pipe.st_dev)},
        {"pipe_inode", w::Unsigned(pipe.st_ino)},
        {"nonce", w::String(nonce)}}), &bytes));
    Need(bytes.size() <= kMaximum, "device admission exceeds bound");
    OwnedFd file(static_cast<int>(syscall(SYS_memfd_create,
        "q4t-device-admission", MFD_CLOEXEC | MFD_ALLOW_SEALING)));
    Need(file.Get() >= 0 && fchmod(file.Get(), 0600) == 0 &&
             write(file.Get(), bytes.data(), bytes.size()) ==
                 static_cast<ssize_t>(bytes.size()) &&
             fcntl(file.Get(), F_ADD_SEALS, kSeals) == 0,
         "seal device admission receipt failed");
    OwnedFd readonly(open(("/proc/self/fd/" + std::to_string(file.Get())).c_str(),
                          O_RDONLY | O_CLOEXEC));
    Need(readonly.Get() >= 0 &&
             write(output.Get(), nonce.data(), nonce.size()) ==
                 static_cast<ssize_t>(nonce.size()),
         "finish device admission token failed");
    output.Reset();  // No writer is inherited; the protected pipe cannot refill.
    *receipt = std::move(readonly);
    *token = std::move(input);
    return {};
  } catch (const std::exception& error) {
    return Status::Fail(error.what());
  }
}

Status InspectDeviceAdmission(int receipt, int token,
                              DeviceAdmission* admission) {
  try {
    Need(admission != nullptr, "missing device admission output");
    *admission = Inspect(receipt, token).claims;
    return {};
  } catch (const std::exception& error) {
    return Status::Fail(error.what());
  }
}

Status ConsumeDeviceAdmission(int receipt, int token,
                              const DeviceAdmission& expected,
                              const DeviceFilter& filter, int lease_fd,
                              const std::string& lease_path, uint64_t now_ns) {
  try {
    const auto record = Inspect(receipt, token);
    Need(record.claims == expected, "device admission deployment changed");
    Need(geteuid() == expected.runtime_uid &&
             now_ns >= expected.issued_ns && now_ns < expected.expires_ns,
         "device admission caller or lifetime invalid");
    Need(filter.Identity() == expected.filter, "device filter binding changed");
    Need(filter.Verify());
    Need(CurrentCgroup() == expected.filter.allowed_cgroup_id,
         "device caller is outside the admitted cgroup");
    Need(CheckInheritedLease(lease_fd, lease_path));
    struct stat lease {};
    Need(fstat(lease_fd, &lease) == 0 &&
             lease.st_uid == expected.runtime_uid && lease.st_nlink == 1 &&
             lease.st_dev == expected.lease_device &&
             lease.st_ino == expected.lease_inode,
         "device admission lease changed");
    pollfd available{token, POLLIN | POLLHUP, 0};
    Need(poll(&available, 1, 0) == 1 && (available.revents & POLLIN) &&
             (available.revents & POLLHUP),
         "device admission token missing, spent or still writable");
    std::array<char, 33> nonce{};
    Need(read(token, nonce.data(), nonce.size()) == 32 &&
             std::string(nonce.data(), 32) == record.nonce,
         "device admission token already consumed or invalid");
    char extra = 0;
    Need(read(token, &extra, 1) == 0, "device admission token has extra data");
    Need(filter.Verify());
    return {};
  } catch (const std::exception& error) {
    return Status::Fail(error.what());
  }
}
}  // namespace q4t::service
