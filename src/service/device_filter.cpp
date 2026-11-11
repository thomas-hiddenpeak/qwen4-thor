#include "q4t/service/device_filter.h"

#include <fcntl.h>
#include <linux/bpf.h>
#include <linux/magic.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/vfs.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <cstring>
#include <filesystem>
#include <set>
#include <string>
#include <utility>

namespace q4t::service {
namespace {
Status Error(const char* operation) {
  return Status::Fail(std::string(operation) + ": " + std::strerror(errno));
}

int Bpf(enum bpf_cmd command, union bpf_attr* attribute) {
  return static_cast<int>(syscall(SYS_bpf, command, attribute,
                                  sizeof(*attribute)));
}

uint64_t Pointer(const void* pointer) {
  return reinterpret_cast<uintptr_t>(pointer);
}

// Original instruction construction using the Linux UAPI. No copied kernel
// implementation or GPL example, no external assembler/compiler at runtime.
bpf_insn Instruction(uint8_t code, uint8_t destination, uint8_t source,
                     int16_t offset, uint32_t immediate) {
  bpf_insn result{};
  result.code = code;
  result.dst_reg = destination;
  result.src_reg = source;
  result.off = offset;
  std::memcpy(&result.imm, &immediate, sizeof(immediate));
  return result;
}

std::vector<bpf_insn> Instructions(
    uint64_t allowed, const std::vector<DeviceNumber>& devices) {
  std::vector<bpf_insn> code;
  auto add = [&](uint8_t opcode, uint8_t dst, uint8_t src, int16_t off,
                 uint32_t imm) {
    code.push_back(Instruction(opcode, dst, src, off, imm));
  };
  add(BPF_LDX | BPF_W | BPF_MEM, 2, 1, 0, 0);  // access_type
  add(BPF_ALU64 | BPF_AND | BPF_K, 2, 0, 0, 0xffff);
  const size_t non_character = code.size();
  add(BPF_JMP | BPF_JNE | BPF_K, 2, 0, 0, BPF_DEVCG_DEV_CHAR);
  add(BPF_LDX | BPF_W | BPF_MEM, 2, 1, 4, 0);  // major
  add(BPF_LDX | BPF_W | BPF_MEM, 3, 1, 8, 0);  // minor
  std::vector<size_t> matches;
  for (const auto& device : devices) {
    add(BPF_JMP | BPF_JNE | BPF_K, 2, 0, 1, device.major);
    matches.push_back(code.size());
    add(BPF_JMP | BPF_JEQ | BPF_K, 3, 0, 0, device.minor);
  }
  const size_t allow = code.size();
  add(BPF_ALU64 | BPF_MOV | BPF_K, 0, 0, 0, 1);
  add(BPF_JMP | BPF_EXIT, 0, 0, 0, 0);
  const size_t check = code.size();
  add(BPF_JMP | BPF_CALL, 0, 0, 0, BPF_FUNC_get_current_cgroup_id);
  add(BPF_LD | BPF_DW | BPF_IMM, 1, 0, 0,
      static_cast<uint32_t>(allowed));
  add(0, 0, 0, 0, static_cast<uint32_t>(allowed >> 32));
  const size_t same = code.size();
  add(BPF_JMP | BPF_JEQ | BPF_X, 0, 1, 0, 0);
  add(BPF_ALU64 | BPF_MOV | BPF_K, 0, 0, 0, 0);
  add(BPF_JMP | BPF_EXIT, 0, 0, 0, 0);
  auto branch = [&](size_t at, size_t target) {
    code[at].off = static_cast<int16_t>(
        static_cast<int>(target) - static_cast<int>(at) - 1);
  };
  branch(non_character, allow);
  branch(same, allow);
  for (size_t at : matches) branch(at, check);
  return code;
}

template <typename T>
Status Info(int fd, T* info) {
  bpf_attr attribute{};
  attribute.info.bpf_fd = fd;
  attribute.info.info_len = sizeof(T);
  attribute.info.info = Pointer(info);
  if (Bpf(BPF_OBJ_GET_INFO_BY_FD, &attribute) < 0)
    return Error("read device filter kernel identity");
  return {};
}

std::string Tag(const bpf_prog_info& info) {
  constexpr char hex[] = "0123456789abcdef";
  std::string result;
  for (unsigned char byte : info.tag) {
    result.push_back(hex[byte >> 4]);
    result.push_back(hex[byte & 15]);
  }
  return result;
}

Status LoadProgram(uint64_t allowed, const std::vector<DeviceNumber>& devices,
                   OwnedFd* output, bpf_prog_info* information) {
  if (devices.empty() || devices.size() > 64)
    return Status::Fail("device list must contain 1 to 64 entries");
  std::set<std::pair<uint32_t, uint32_t>> unique;
  for (const auto& device : devices) {
    // Linux encodes a 12-bit major and 20-bit minor in dev_t.
    if (device.major == 0 || device.major >= (1U << 12) ||
        device.minor >= (1U << 20) ||
        !unique.emplace(device.major, device.minor).second)
      return Status::Fail("invalid or repeated device number");
  }
  auto code = Instructions(allowed, devices);
  std::array<char, 65536> log{};
  constexpr char license[] = "MIT";
  bpf_attr load{};
  load.prog_type = BPF_PROG_TYPE_CGROUP_DEVICE;
  load.expected_attach_type = BPF_CGROUP_DEVICE;
  load.insn_cnt = static_cast<uint32_t>(code.size());
  load.insns = Pointer(code.data());
  load.license = Pointer(license);
  load.log_level = 1;
  load.log_size = log.size();
  load.log_buf = Pointer(log.data());
  std::strcpy(load.prog_name, "q4t_device");
  OwnedFd program(Bpf(BPF_PROG_LOAD, &load));
  if (program.Get() < 0) {
    const std::string reason = std::strerror(errno);
    return Status::Fail("load device filter: " + reason + "; " +
                        std::string(log.data(), strnlen(log.data(), log.size())));
  }
  bpf_prog_info program_info{};
  auto status = Info(program.Get(), &program_info);
  if (!status.ok()) return status;
  if (program_info.id == 0 ||
      program_info.type != BPF_PROG_TYPE_CGROUP_DEVICE)
    return Status::Fail("unexpected device program identity");
  *information = program_info;
  *output = std::move(program);
  return {};
}

struct PinLocation {
  OwnedFd parent;
  std::string name;
  std::string reference;
};

Status LocatePin(const std::string& path, PinLocation* location) {
  const std::filesystem::path value(path);
  const auto name = value.filename().string();
  if (!value.is_absolute() || name.empty() || name == "." || name == ".." ||
      name.size() > 128 || name.find_first_not_of(
          "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_") !=
          std::string::npos)
    return Status::Fail("invalid device pin name");
  OwnedFd parent(open(value.parent_path().c_str(),
                      O_RDONLY | O_CLOEXEC | O_DIRECTORY | O_NOFOLLOW));
  if (parent.Get() < 0) return Error("open device pin directory");
  struct stat identity {};
  struct statfs filesystem {};
  if (fstat(parent.Get(), &identity) != 0 ||
      fstatfs(parent.Get(), &filesystem) != 0)
    return Error("inspect device pin directory");
  if (filesystem.f_type != BPF_FS_MAGIC || identity.st_uid != 0 ||
      (identity.st_mode & 0022) != 0)
    return Status::Fail("device pin directory must be root-owned private bpffs");
  location->name = name;
  location->reference = "/proc/self/fd/" + std::to_string(parent.Get()) +
                        "/" + name;
  location->parent = std::move(parent);
  return {};
}

Status GetPin(const PinLocation& location, OwnedFd* output) {
  struct stat identity {};
  if (fstatat(location.parent.Get(), location.name.c_str(), &identity,
              AT_SYMLINK_NOFOLLOW) != 0)
    return Error("inspect device pin");
  if (!S_ISREG(identity.st_mode) || identity.st_uid != 0 ||
      (identity.st_mode & 0022) != 0)
    return Status::Fail("device pin is not root-owned and protected");
  bpf_attr attribute{};
  attribute.pathname = Pointer(location.reference.c_str());
  OwnedFd fd(Bpf(BPF_OBJ_GET, &attribute));
  if (fd.Get() < 0) return Error("open device pin");
  *output = std::move(fd);
  return {};
}

Status CheckDescendant(int target, int allowed) {
  uint64_t target_id = 0;
  auto status = ReadCgroupId(target, &target_id);
  if (!status.ok()) return status;
  OwnedFd current(fcntl(allowed, F_DUPFD_CLOEXEC, 3));
  if (current.Get() < 0) return Error("duplicate allowed cgroup");
  for (int depth = 0; depth < 64; ++depth) {
    uint64_t before = 0;
    status = ReadCgroupId(current.Get(), &before);
    if (!status.ok()) return status;
    OwnedFd parent(openat(current.Get(), "..",
                          O_RDONLY | O_CLOEXEC | O_DIRECTORY | O_NOFOLLOW));
    uint64_t after = 0;
    status = ReadCgroupId(parent.Get(), &after);
    if (!status.ok()) return status;
    if (after == target_id) return {};
    if (after == before) break;
    current = std::move(parent);
  }
  return Status::Fail("allowed device cgroup is not below filter target");
}
}  // namespace

Status ReadCgroupId(int fd, uint64_t* id) {
  if (fd < 0 || id == nullptr) return Status::Fail("invalid cgroup input");
  struct stat identity {};
  struct statfs filesystem {};
  if (fstat(fd, &identity) != 0 || fstatfs(fd, &filesystem) != 0)
    return Error("inspect device cgroup");
  if (!S_ISDIR(identity.st_mode) ||
      filesystem.f_type != CGROUP2_SUPER_MAGIC || identity.st_ino == 0)
    return Status::Fail("device domain is not cgroup v2");
  *id = identity.st_ino;
  return {};
}

Status DeviceFilter::Attach(int target_cgroup, int allowed_cgroup,
                            const std::vector<DeviceNumber>& devices) {
  if (link_.Get() >= 0 || program_.Get() >= 0)
    return Status::Fail("device filter already attached");
  DeviceFilterIdentity identity;
  auto status = ReadCgroupId(target_cgroup, &identity.target_cgroup_id);
  if (!status.ok()) return status;
  status = ReadCgroupId(allowed_cgroup, &identity.allowed_cgroup_id);
  if (!status.ok()) return status;
  if (identity.target_cgroup_id == identity.allowed_cgroup_id)
    return Status::Fail("allowed device group must differ from filter target");
  status = CheckDescendant(target_cgroup, allowed_cgroup);
  if (!status.ok()) return status;
  OwnedFd program;
  bpf_prog_info program_info{};
  status = LoadProgram(identity.allowed_cgroup_id, devices, &program,
                        &program_info);
  if (!status.ok()) return status;
  identity.program_tag = Tag(program_info);
  identity.program_id = program_info.id;
  bpf_attr attach{};
  attach.link_create.prog_fd = program.Get();
  attach.link_create.target_fd = target_cgroup;
  attach.link_create.attach_type = BPF_CGROUP_DEVICE;
  OwnedFd link(Bpf(BPF_LINK_CREATE, &attach));
  if (link.Get() < 0) return Error("attach device filter link");
  bpf_link_info link_info{};
  status = Info(link.Get(), &link_info);
  if (!status.ok()) return status;
  identity.link_id = link_info.id;
  if (identity.link_id == 0 || link_info.type != BPF_LINK_TYPE_CGROUP ||
      link_info.prog_id != identity.program_id ||
      link_info.cgroup.cgroup_id != identity.target_cgroup_id ||
      link_info.cgroup.attach_type != BPF_CGROUP_DEVICE)
    return Status::Fail("unexpected device link identity");
  identity_ = identity;
  program_ = std::move(program);
  link_ = std::move(link);
  return {};
}

Status DeviceFilter::Verify() const {
  if (link_.Get() < 0 || program_.Get() < 0)
    return Status::Fail("device filter has no custody");
  bpf_link_info link{};
  auto status = Info(link_.Get(), &link);
  if (!status.ok()) return status;
  bpf_prog_info program{};
  status = Info(program_.Get(), &program);
  if (!status.ok()) return status;
  if (link.type != BPF_LINK_TYPE_CGROUP || link.id != identity_.link_id ||
      link.prog_id != identity_.program_id ||
      link.cgroup.cgroup_id != identity_.target_cgroup_id ||
      link.cgroup.attach_type != BPF_CGROUP_DEVICE ||
      program.type != BPF_PROG_TYPE_CGROUP_DEVICE ||
      program.id != identity_.program_id ||
      Tag(program) != identity_.program_tag)
    return Status::Fail("device filter kernel binding changed");
  return {};
}

Status DeviceFilter::Pin(const std::string& path) const {
  auto status = Verify();
  if (!status.ok()) return status;
  PinLocation location;
  status = LocatePin(path, &location);
  if (!status.ok()) return status;
  bpf_attr attribute{};
  attribute.bpf_fd = link_.Get();
  attribute.pathname = Pointer(location.reference.c_str());
  // BPF_OBJ_PIN is exclusive; an existing object is never overwritten.
  if (Bpf(BPF_OBJ_PIN, &attribute) < 0) return Error("pin device filter");
  return {};
}

Status DeviceFilter::AdoptPinned(
    const std::string& path, int target_cgroup, int allowed_cgroup,
    const std::vector<DeviceNumber>& devices,
    const DeviceFilterIdentity& expected) {
  if (link_.Get() >= 0 || program_.Get() >= 0)
    return Status::Fail("device filter already attached");
  DeviceFilterIdentity identity;
  auto status = ReadCgroupId(target_cgroup, &identity.target_cgroup_id);
  if (!status.ok()) return status;
  status = ReadCgroupId(allowed_cgroup, &identity.allowed_cgroup_id);
  if (!status.ok()) return status;
  if (identity.target_cgroup_id != expected.target_cgroup_id ||
      identity.allowed_cgroup_id != expected.allowed_cgroup_id ||
      identity.target_cgroup_id == identity.allowed_cgroup_id)
    return Status::Fail("device adoption cgroup identity changed");
  status = CheckDescendant(target_cgroup, allowed_cgroup);
  if (!status.ok()) return status;
  PinLocation location;
  status = LocatePin(path, &location);
  if (!status.ok()) return status;
  OwnedFd link;
  status = GetPin(location, &link);
  if (!status.ok()) return status;
  bpf_link_info observed{};
  status = Info(link.Get(), &observed);
  if (!status.ok()) return status;
  if (observed.type != BPF_LINK_TYPE_CGROUP ||
      observed.cgroup.attach_type != BPF_CGROUP_DEVICE ||
      observed.id != expected.link_id || observed.prog_id != expected.program_id ||
      observed.cgroup.cgroup_id != identity.target_cgroup_id)
    return Status::Fail("pinned device link does not match retained identity");
  bpf_attr get_program{};
  get_program.prog_id = observed.prog_id;
  OwnedFd program(Bpf(BPF_PROG_GET_FD_BY_ID, &get_program));
  if (program.Get() < 0) return Error("open retained device program");
  bpf_prog_info actual{}, known{};
  status = Info(program.Get(), &actual);
  if (!status.ok()) return status;
  OwnedFd unattached;
  status = LoadProgram(identity.allowed_cgroup_id, devices, &unattached, &known);
  if (!status.ok()) return status;
  if (actual.type != BPF_PROG_TYPE_CGROUP_DEVICE ||
      actual.id != expected.program_id || Tag(actual) != Tag(known) ||
      Tag(actual) != expected.program_tag)
    return Status::Fail("retained device program differs from expected rules");
  identity_ = expected;
  link_ = std::move(link);
  program_ = std::move(program);
  return Verify();
}

Status DeviceFilter::RemovePin(const std::string& path) const {
  auto status = Verify();
  if (!status.ok()) return status;
  PinLocation location;
  status = LocatePin(path, &location);
  if (!status.ok()) return status;
  OwnedFd retained;
  status = GetPin(location, &retained);
  if (!status.ok()) return status;
  bpf_link_info info{};
  status = Info(retained.Get(), &info);
  if (!status.ok()) return status;
  if (info.id != identity_.link_id || info.prog_id != identity_.program_id ||
      info.type != BPF_LINK_TYPE_CGROUP ||
      info.cgroup.cgroup_id != identity_.target_cgroup_id)
    return Status::Fail("refuse to remove another device filter pin");
  // The caller holds its root-owned deployment lock across removal. Closing
  // this registration never removes another attached kernel rule.
  if (unlinkat(location.parent.Get(), location.name.c_str(), 0) != 0)
    return Error("remove owned device filter pin");
  return {};
}
}  // namespace q4t::service
