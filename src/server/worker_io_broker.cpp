#include "worker_io_broker.h"

#include <fcntl.h>
#include <linux/audit.h>
#include <linux/seccomp.h>
#include <poll.h>
#include <sched.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <new>
#include <utility>

#include "q4t/server/grammar_process.h"

namespace q4t::server::detail {
namespace {
constexpr char kListenerTag = 'B';
bool Charge(uint64_t amount, uint64_t* remaining) {
  if (amount > *remaining) return false;
  *remaining -= amount;
  return true;
}
}  // namespace

WorkerIoBroker::WorkerIoBroker(int descriptor, WorkerIoBudget budget)
    : descriptor_(descriptor), remaining_(budget) {}
WorkerIoBroker::~WorkerIoBroker() {
  if (descriptor_ >= 0) close(descriptor_);
}
bool WorkerIoBroker::Valid(const WorkerIoBudget& budget) {
  return budget.write_bytes <= (1ULL << 30) && budget.file_creations <= 65536 &&
         budget.thread_creations <= 256 && budget.notifications > 0 &&
         budget.notifications <= 2000000;
}
WorkerIoBroker::Result WorkerIoBroker::Pump() {
  // Poll before each receive, with one reader only. On Linux 6.8 withdrawal
  // leaves the request counter for this reader to consume (ENOENT); no other
  // reader can consume the polled notification and leave us waiting forever.
  pollfd ready{descriptor_, POLLIN, 0};
  const int count = poll(&ready, 1, 0);
  if (count < 0) return errno == EINTR ? Result::kOk : Result::kInvalid;
  if (!count || !(ready.revents & POLLIN))
    return (ready.revents & (POLLERR | POLLNVAL)) ? Result::kInvalid
                                                  : Result::kOk;
  seccomp_notif_sizes sizes{};
  if (syscall(SYS_seccomp, SECCOMP_GET_NOTIF_SIZES, 0, &sizes) ||
      sizes.seccomp_notif < sizeof(seccomp_notif) ||
      sizes.seccomp_notif > 4096 ||
      sizes.seccomp_notif_resp < sizeof(seccomp_notif_resp) ||
      sizes.seccomp_notif_resp > 4096)
    return Result::kInvalid;
  // Allocate for the running kernel's extensible structures. Zero tails are
  // required; no decoder-controlled size is accepted.
  alignas(seccomp_notif) std::array<unsigned char, 4096> request_bytes{};
  auto* request = reinterpret_cast<seccomp_notif*>(request_bytes.data());
  if (ioctl(descriptor_, SECCOMP_IOCTL_NOTIF_RECV, request)) {
    if (errno == ENOENT || errno == EINTR) return Result::kOk;
    return Result::kInvalid;
  }
#if defined(__aarch64__)
  constexpr uint32_t architecture = AUDIT_ARCH_AARCH64;
#elif defined(__x86_64__)
  constexpr uint32_t architecture = AUDIT_ARCH_X86_64;
#else
  constexpr uint32_t architecture = 0;
#endif
  if (!architecture || request->data.arch != architecture)
    return Result::kInvalid;
  if (!Charge(1, &remaining_.notifications)) return Result::kCapacity;
  const auto& args = request->data.args;
  const int call = request->data.nr;
  bool allowed = false;
  if (call == SYS_write || call == SYS_pwrite64)
    allowed = Charge(args[2], &remaining_.write_bytes);
  else if (call == SYS_ftruncate)
    // Conservative cumulative declared size, not net growth. No refund for
    // short writes, failed attempts, truncation, unlink, or sparse files.
    allowed = Charge(args[1], &remaining_.write_bytes);
  else if (call == SYS_openat)
    allowed = !(args[2] & (O_CREAT | (O_TMPFILE & ~O_DIRECTORY))) ||
              Charge(1, &remaining_.file_creations);
  else if (call == SYS_mkdirat)
    allowed = Charge(1, &remaining_.file_creations);
#if defined(__x86_64__)
  else if (call == SYS_open)
    allowed = !(args[1] & (O_CREAT | (O_TMPFILE & ~O_DIRECTORY))) ||
              Charge(1, &remaining_.file_creations);
  else if (call == SYS_mkdir)
    allowed = Charge(1, &remaining_.file_creations);
#endif
  else if (call == SYS_clone) {
    constexpr uint64_t required = CLONE_VM | CLONE_SIGHAND | CLONE_THREAD;
    constexpr uint64_t permitted =
        required | CLONE_FS | CLONE_FILES | CLONE_SYSVSEM | CLONE_SETTLS |
        CLONE_PARENT_SETTID | CLONE_CHILD_CLEARTID | CLONE_CHILD_SETTID;
    if ((args[0] & required) != required || (args[0] & ~permitted))
      return Result::kInvalid;
    allowed = Charge(1, &remaining_.thread_creations);
  } else
    return Result::kInvalid;
  if (!allowed) return Result::kCapacity;
  alignas(seccomp_notif_resp) std::array<unsigned char, 4096> response_bytes{};
  auto* response = reinterpret_cast<seccomp_notif_resp*>(response_bytes.data());
  response->id = request->id;
  response->flags = SECCOMP_USER_NOTIF_FLAG_CONTINUE;
  if (ioctl(descriptor_, SECCOMP_IOCTL_NOTIF_SEND, response) && errno != ENOENT)
    return Result::kInvalid;
  return Result::kOk;
}

bool SendWorkerIoListener(int listener) {
  char tag = kListenerTag;
  iovec payload{&tag, 1};
  alignas(cmsghdr) char ancillary[CMSG_SPACE(sizeof(int))]{};
  msghdr message{};
  message.msg_iov = &payload;
  message.msg_iovlen = 1;
  message.msg_control = ancillary;
  message.msg_controllen = sizeof(ancillary);
  auto* control = CMSG_FIRSTHDR(&message);
  control->cmsg_level = SOL_SOCKET;
  control->cmsg_type = SCM_RIGHTS;
  control->cmsg_len = CMSG_LEN(sizeof(int));
  std::memcpy(CMSG_DATA(control), &listener, sizeof(listener));
  return sendmsg(3, &message, MSG_NOSIGNAL) == 1;
}

bool GrammarProcess::PumpIoBudget() {
  if (!io_broker_) return true;
  const auto result = io_broker_->Pump();
  if (result == WorkerIoBroker::Result::kOk) return true;
  failure_ = result == WorkerIoBroker::Result::kCapacity
                 ? GrammarFailure::kResource
                 : GrammarFailure::kWorker;
  Stop();
  return false;
}

Status GrammarProcess::ReceiveIoBudget(
    const WorkerIoBudget& budget,
    std::chrono::steady_clock::time_point deadline,
    const std::function<bool()>& cancelled) {
  if (io_broker_ || !WorkerIoBroker::Valid(budget)) {
    failure_ = GrammarFailure::kResource;
    Stop();
    return Status::Fail("invalid worker IO budget");
  }
  while (socket_ >= 0 && pid_ > 0) {
    if ((cancelled && cancelled()) ||
        (control_.cancelled && control_.cancelled())) {
      failure_ = GrammarFailure::kCancelled;
      Stop();
      return Status::Fail("worker listener transfer cancelled");
    }
    const auto remaining = deadline - std::chrono::steady_clock::now();
    if (remaining <= decltype(remaining)::zero()) {
      failure_ = GrammarFailure::kTimeout;
      Stop();
      return Status::Fail("worker listener transfer timed out");
    }
    pollfd ready{socket_, POLLIN, 0};
    const int timeout = static_cast<int>(std::clamp<int64_t>(
        std::chrono::duration_cast<std::chrono::milliseconds>(remaining)
            .count(),
        1, 10));
    const int result = poll(&ready, 1, timeout);
    if (result < 0 && errno == EINTR) continue;
    if (result < 0) break;
    if (!result) continue;
    char tag = 0;
    iovec payload{&tag, 1};
    alignas(cmsghdr) char ancillary[CMSG_SPACE(sizeof(int) * 8)]{};
    msghdr message{};
    message.msg_iov = &payload;
    message.msg_iovlen = 1;
    message.msg_control = ancillary;
    message.msg_controllen = sizeof(ancillary);
    const auto received =
        recvmsg(socket_, &message, MSG_DONTWAIT | MSG_CMSG_CLOEXEC);
    if (received < 0 && (errno == EINTR || errno == EAGAIN)) continue;
    std::array<int, 8> descriptors{};
    size_t descriptor_count = 0;
    bool valid = received == 1 && tag == kListenerTag &&
                 !(message.msg_flags & (MSG_TRUNC | MSG_CTRUNC));
    for (auto* control = CMSG_FIRSTHDR(&message); control;
         control = CMSG_NXTHDR(&message, control)) {
      if (control->cmsg_level != SOL_SOCKET ||
          control->cmsg_type != SCM_RIGHTS || control->cmsg_len < CMSG_LEN(0)) {
        valid = false;
        continue;
      }
      const auto bytes = control->cmsg_len - CMSG_LEN(0);
      if (bytes % sizeof(int)) valid = false;
      for (size_t offset = 0; offset + sizeof(int) <= bytes;
           offset += sizeof(int)) {
        int fd;
        std::memcpy(&fd, CMSG_DATA(control) + offset, sizeof(fd));
        if (descriptor_count < descriptors.size())
          descriptors[descriptor_count++] = fd;
        else {
          close(fd);
          valid = false;
        }
      }
    }
    if (valid && descriptor_count == 1) {
      // Probe the kernel interface without consuming or changing any request.
      uint64_t id = 0;
      const int probe =
          ioctl(descriptors[0], SECCOMP_IOCTL_NOTIF_ID_VALID, &id);
      if (probe == 0 || (probe == -1 && errno == ENOENT)) {
        io_broker_.reset(new (std::nothrow)
                             WorkerIoBroker(descriptors[0], budget));
        if (io_broker_) return {};
      }
    }
    for (size_t i = 0; i < descriptor_count; ++i) close(descriptors[i]);
    break;
  }
  failure_ = GrammarFailure::kWorker;
  Stop();
  return Status::Fail("invalid worker listener transfer");
}
}  // namespace q4t::server::detail
