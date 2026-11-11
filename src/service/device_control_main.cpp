// Disposable privileged syscall adapter. Only an explicit root deployment
// process can call it via its inherited private socket and held lease. There
// is no listener, daemon, setuid install, CUDA/NVML access or model command.
#include <fcntl.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <charconv>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

#include "q4t/service/device_admission.h"
#include "q4t/service/guardian_wire.h"

namespace {
namespace s = q4t::service;
namespace w = s::wire;
using q4t::io::Json;
constexpr size_t kMaximum = 65536;

void Need(bool value, const char* reason) {
  if (!value) throw std::runtime_error(reason);
}
void Need(const q4t::Status& value) {
  if (!value.ok()) throw std::runtime_error(value.message());
}
void Fields(const Json& value, std::initializer_list<std::string> fields) {
  Need(w::HasKeys(value, fields), "invalid device control fields");
}
uint64_t Number(const Json& value, const char* name) {
  uint64_t result = 0;
  Need(w::GetUnsigned(value, name, &result));
  return result;
}
int Descriptor(const Json& value, const char* name) {
  const auto result = Number(value, name);
  Need(result >= 3 && result <= 1000000, "invalid device control descriptor");
  return static_cast<int>(result);
}
std::string Text(const Json& value, const char* name) {
  const auto* field = value.Find(name);
  Need(field && field->IsString() && field->str.size() <= 4096 &&
           field->str.find('\0') == std::string::npos,
       "invalid device control text");
  return field->str;
}
Json Identity(const s::DeviceFilterIdentity& value) {
  return w::Object({{"program_id", w::Unsigned(value.program_id)},
      {"link_id", w::Unsigned(value.link_id)},
      {"target_cgroup_id", w::Unsigned(value.target_cgroup_id)},
      {"allowed_cgroup_id", w::Unsigned(value.allowed_cgroup_id)},
      {"program_tag", w::String(value.program_tag)}});
}
s::DeviceFilterIdentity ParseIdentity(const Json& value) {
  Fields(value, {"program_id", "link_id", "target_cgroup_id",
                 "allowed_cgroup_id", "program_tag"});
  const auto program = Number(value, "program_id");
  const auto link = Number(value, "link_id");
  Need(program > 0 && program <= UINT32_MAX && link > 0 && link <= UINT32_MAX,
       "invalid kernel object ID");
  return {static_cast<uint32_t>(program), static_cast<uint32_t>(link),
      Number(value, "target_cgroup_id"), Number(value, "allowed_cgroup_id"),
      Text(value, "program_tag")};
}
std::vector<s::DeviceNumber> Devices(const Json& value) {
  Need(value.IsArray() && !value.array.empty() && value.array.size() <= 64,
       "invalid device control rules");
  std::vector<s::DeviceNumber> result;
  for (const auto& rule : value.array) {
    Fields(rule, {"major", "minor", "all_minors"});
    const auto major = Number(rule, "major"), minor = Number(rule, "minor");
    const auto* all = rule.Find("all_minors");
    Need(major < 4096 && minor < (1U << 20) && all->IsBool(),
         "invalid device control number");
    result.push_back({static_cast<uint32_t>(major), static_cast<uint32_t>(minor),
                      all->boolean});
  }
  return result;
}
void Send(int socket, const Json& result, const std::vector<int>& fds = {}) {
  std::string bytes;
  Need(w::Encode(result, &bytes));
  Need(bytes.size() <= kMaximum && fds.size() <= 4,
       "device control response exceeds bound");
  iovec part{bytes.data(), bytes.size()};
  alignas(cmsghdr) std::array<char, CMSG_SPACE(4*sizeof(int))> control{};
  msghdr message{};
  message.msg_iov = &part;
  message.msg_iovlen = 1;
  if (!fds.empty()) {
    message.msg_control = control.data();
    message.msg_controllen = CMSG_SPACE(fds.size()*sizeof(int));
    auto* header = CMSG_FIRSTHDR(&message);
    header->cmsg_level = SOL_SOCKET;
    header->cmsg_type = SCM_RIGHTS;
    header->cmsg_len = CMSG_LEN(fds.size()*sizeof(int));
    std::memcpy(CMSG_DATA(header), fds.data(), fds.size()*sizeof(int));
  }
  Need(sendmsg(socket, &message, MSG_DONTWAIT | MSG_NOSIGNAL) ==
           static_cast<ssize_t>(bytes.size()), "send device control result failed");
}

void Execute(int socket, const Json& command) {
  Fields(command, {"version", "op", "lease_fd", "lease_path", "args"});
  Need(Number(command, "version") == 1, "unknown device control protocol");
  const int lease = Descriptor(command, "lease_fd");
  const auto lease_path = Text(command, "lease_path");
  Need(s::CheckInheritedLease(lease, lease_path));
  const auto operation = Text(command, "op");
  const auto& args = *command.Find("args");
  s::DeviceFilter filter;
  if (operation == "issue") {
    Fields(args, {"link_fd", "program_fd", "admission"});
    std::string bytes;
    Need(w::Encode(*args.Find("admission"), &bytes));
    s::DeviceAdmission admission;
    Need(s::ParseDeviceAdmission(bytes, &admission));
    Need(filter.AdoptInherited(Descriptor(args, "link_fd"),
                               Descriptor(args, "program_fd"), admission.filter));
    s::OwnedFd receipt, token;
    Need(s::IssueDeviceAdmission(admission, filter, lease, lease_path,
                                &receipt, &token));
    Send(socket, w::Object({{"ok", w::Boolean(true)},
                           {"result", w::String("issued")}}),
         {receipt.Get(), token.Get()});
    return;
  }
  if (operation == "attach" || operation == "recover") {
    Fields(args, {"target_fd", "allowed_fd", "devices", "pin"});
    if (operation == "attach") {
      Need(filter.Attach(Descriptor(args, "target_fd"),
                          Descriptor(args, "allowed_fd"),
                          Devices(*args.Find("devices"))));
      Need(filter.Pin(Text(args, "pin")));
    } else {
      Need(filter.RecoverPinned(Text(args, "pin"),
          Descriptor(args, "target_fd"), Descriptor(args, "allowed_fd"),
          Devices(*args.Find("devices"))));
    }
  } else if (operation == "adopt" || operation == "remove") {
    Fields(args, {"target_fd", "allowed_fd", "devices", "pin", "identity"});
    Need(filter.AdoptPinned(Text(args, "pin"), Descriptor(args, "target_fd"),
        Descriptor(args, "allowed_fd"), Devices(*args.Find("devices")),
        ParseIdentity(*args.Find("identity"))));
    if (operation == "remove") {
      Need(filter.RemovePin(Text(args, "pin")));
      Send(socket, w::Object({{"ok", w::Boolean(true)},
                             {"result", w::String("removed")}}));
      return;
    }
  } else {
    throw std::runtime_error("unknown device control operation");
  }
  Send(socket, w::Object({{"ok", w::Boolean(true)},
                         {"result", Identity(filter.Identity())}}),
       {filter.LinkFd(), filter.ProgramFd()});
}
}  // namespace

int main(int argc, char** argv) {
  int socket = -1;
  if (geteuid() != 0 || argc != 3 || std::string(argv[1]) != "--fd") return 2;
  const auto input = std::string(argv[2]);
  const auto parsed = std::from_chars(input.data(), input.data()+input.size(), socket);
  if (parsed.ec != std::errc{} || parsed.ptr != input.data()+input.size() ||
      socket < 3 || socket > 1000000) return 2;
  try {
    ucred peer{};
    socklen_t length = sizeof(peer);
    int type = 0;
    socklen_t type_length = sizeof(type);
    const auto parent = getppid();
    Need(getsockopt(socket, SOL_SOCKET, SO_TYPE, &type, &type_length) == 0 &&
             type == SOCK_SEQPACKET &&
             getsockopt(socket, SOL_SOCKET, SO_PEERCRED, &peer, &length) == 0 &&
             length == sizeof(peer) && peer.uid == 0 && peer.pid == parent,
         "device control requires private privileged parent");
    Need(prctl(PR_SET_PDEATHSIG, SIGKILL) == 0 && getppid() == parent,
         "device control parent unavailable");
    std::array<char, kMaximum> bytes{};
    iovec part{bytes.data(), bytes.size()};
    msghdr message{};
    message.msg_iov = &part;
    message.msg_iovlen = 1;
    const auto count = recvmsg(socket, &message, MSG_DONTWAIT);
    Need(count > 0 && !(message.msg_flags & (MSG_TRUNC | MSG_CTRUNC)),
         "device control request unavailable or oversized");
    Json value;
    Need(w::Parse(std::string(bytes.data(), count), &value));
    Execute(socket, value);
    return 0;
  } catch (const std::exception& error) {
    try {
      Send(socket, w::Object({{"ok", w::Boolean(false)},
          {"error", w::String(std::string(error.what()).substr(0, 2048))}}));
    } catch (...) {}
    return 1;
  }
}
