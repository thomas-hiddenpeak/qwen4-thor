#include "q4t/service/guardian_journal.h"

#include <fcntl.h>
#include <signal.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <limits>
#include <vector>

#include "q4t/service/guardian_wire.h"

namespace q4t::service {
namespace {
constexpr int64_t kMaxTimeout = 300'000'000'000;
constexpr uint64_t kMaxInteger = std::numeric_limits<int64_t>::max();

Status TransactionId(std::string* result) {
  std::array<unsigned char, 16> bytes;
  ssize_t count;
  do {
    count = getrandom(bytes.data(), bytes.size(), GRND_NONBLOCK);
  } while (count < 0 && errno == EINTR);
  if (count != static_cast<ssize_t>(bytes.size())) {
    return Status::Fail("transaction randomness unavailable");
  }
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  constexpr char kHex[] = "0123456789abcdef";
  result->clear();
  for (size_t i = 0; i < bytes.size(); ++i) {
    if (i == 4 || i == 6 || i == 8 || i == 10) result->push_back('-');
    result->push_back(kHex[bytes[i] >> 4]);
    result->push_back(kHex[bytes[i] & 15]);
  }
  return Status{};
}

bool ValidTime(int64_t now, int64_t timeout) {
  return now >= 0 && timeout >= 0 && timeout <= kMaxTimeout &&
         now <= std::numeric_limits<int64_t>::max() - timeout;
}
}  // namespace

JournalClient::~JournalClient() {
  // The owning Guardian normally drives BeginStop/Tick to confirmed reap.
  // Destruction cannot block; unexpected teardown only signals this own child.
  if (!reaped_ && worker_.pidfd.Get() >= 0) {
    SignalProcess(worker_.pidfd.Get(), SIGKILL);
  }
}

Status JournalClient::Freeze(std::string reason) {
  if (failure_.empty()) failure_ = std::move(reason);
  state_ = JournalState::kFrozen;
  reply_.reset();
  socket_.Reset();
  return Status::Fail(failure_);
}

Status JournalClient::Start(const std::string& python,
                            const std::string& script,
                            const std::string& directory,
                            const std::string& service_id, int64_t now_ns,
                            int64_t timeout_ns, JournalDomain domain) {
  if (state_ != JournalState::kNew || !ValidTime(now_ns, timeout_ns) ||
      timeout_ns == 0 || service_id.size() != 36) {
    return Status::Fail("invalid journal startup");
  }
  state_ = JournalState::kStarting;
  domain_ = domain;
  service_id_ = service_id;
  timeout_ns_ = timeout_ns;
  last_now_ns_ = now_ns;
  deadline_ns_ = now_ns + timeout_ns;
  int pair[2];
  if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, pair) != 0) {
    return Freeze("journal socketpair failed");
  }
  socket_.Reset(pair[0]);
  OwnedFd child_socket(pair[1]);
  if (fcntl(socket_.Get(), F_SETFL, O_NONBLOCK) != 0) {
    return Freeze("journal channel nonblocking mode unavailable");
  }
  ProcessRecord self;
  auto status = ReadProcess(getpid(), &self);
  if (!status.ok()) return Freeze(status.message());
  const std::vector<std::string> argv{python,
                                      "-I",
                                      "-B",
                                      script,
                                      "--fd",
                                      std::to_string(child_socket.Get()),
                                      "--parent-pid",
                                      std::to_string(self.identity.pid),
                                      "--parent-start",
                                      std::to_string(self.identity.start_ticks),
                                      "--directory",
                                      directory,
                                      "--service-id",
                                      service_id};
  status = SpawnProcess(argv, {}, {child_socket.Get()}, &worker_);
  if (!status.ok()) return Freeze(status.message());
  return Status{};
}

Status JournalClient::Submit(const std::string& operation, const io::Json& args,
                             int64_t now_ns, uint64_t* sequence) {
  if (state_ != JournalState::kReady || reply_ || sequence == nullptr) {
    return Status::Fail("journal not ready for a new transaction");
  }
  if (now_ns < last_now_ns_ || !ValidTime(now_ns, timeout_ns_) ||
      sequence_ == kMaxInteger || revision_ == kMaxInteger) {
    return Freeze("journal clock or sequence overflow");
  }
  if (!args.IsObject() || operation.size() > 32) {
    return Status::Fail("invalid journal operation");
  }
  auto status = TransactionId(&request_id_);
  if (!status.ok()) return Freeze(status.message());
  using namespace wire;
  const auto request = Object({{"id", String(request_id_)},
                               {"expected_revision", Unsigned(revision_)},
                               {"operation", String(operation)},
                               {"args", args}});
  std::string transaction;
  status = Encode(request, &transaction);
  if (!status.ok() || transaction.size() > 4096) {
    return Status::Fail("journal transaction exceeds limit");
  }
  const auto message = Object({{"version", Unsigned(1)},
                               {"sequence", Unsigned(sequence_ + 1)},
                               {"request", request}});
  std::string encoded;
  status = Encode(message, &encoded);
  if (!status.ok()) return Freeze(status.message());
  const ssize_t sent = send(socket_.Get(), encoded.data(), encoded.size(),
                            MSG_DONTWAIT | MSG_NOSIGNAL);
  if (sent != static_cast<ssize_t>(encoded.size())) {
    return Freeze("journal channel send unavailable");
  }
  *sequence = ++sequence_;
  last_now_ns_ = now_ns;
  deadline_ns_ = now_ns + timeout_ns_;
  state_ = JournalState::kBusy;
  return Status{};
}

Status JournalClient::Receive() {
  std::vector<char> bytes(wire::kMaxFrame);
  iovec vector{bytes.data(), bytes.size()};
  msghdr header{};
  header.msg_iov = &vector;
  header.msg_iovlen = 1;
  const ssize_t count = recvmsg(socket_.Get(), &header, MSG_DONTWAIT);
  if (count < 0 &&
      (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
    return Status{};
  }
  if (count <= 0 || (header.msg_flags & (MSG_TRUNC | MSG_CTRUNC))) {
    return Freeze("journal channel lost or truncated");
  }
  io::Json message;
  auto status = wire::Parse(std::string(bytes.data(), count), &message);
  if (!status.ok()) return Freeze(status.message());
  uint64_t version = 0, sequence = 0;
  if (!wire::GetUnsigned(message, "version", &version).ok() || version != 1 ||
      !wire::GetUnsigned(message, "sequence", &sequence).ok() ||
      sequence != sequence_)
    return Freeze("journal reply identity mismatch");
  const auto* ok = message.Find("ok");
  if (ok == nullptr || !ok->IsBool() || !ok->boolean) {
    return Freeze("journal refused durable authority");
  }
  if (state_ == JournalState::kStarting) {
    if (!wire::HasKeys(message, {"version", "sequence", "ok", "snapshot"})) {
      return Freeze("invalid journal handshake");
    }
    const auto& snapshot = *message.Find("snapshot");
    const char* policy = domain_ == JournalDomain::kVersion
                             ? "q4t-version-selection-v1"
                             : "q4t-recovery-v1";
    if (snapshot.GetString("service_id") != service_id_ ||
        snapshot.GetString("policy") != policy ||
        !wire::GetUnsigned(snapshot, "revision", &revision_).ok()) {
      return Freeze("journal snapshot identity mismatch");
    }
    if (domain_ == JournalDomain::kVersion) {
      uint64_t revision = 0;
      const auto* state = snapshot.Find("state");
      if (!wire::HasKeys(snapshot,
                         {"service_id", "policy", "revision", "state"}) ||
          state == nullptr || state->GetString("format") != policy ||
          !wire::GetUnsigned(*state, "revision", &revision).ok() ||
          revision != revision_) {
        return Freeze("version journal snapshot contract mismatch");
      }
    }
    snapshot_ = snapshot;
    state_ = JournalState::kReady;
    return Status{};
  }
  if (state_ != JournalState::kBusy ||
      !wire::HasKeys(message, {"version", "sequence", "ok", "receipt"})) {
    return Freeze("unsolicited journal reply");
  }
  const auto& receipt = *message.Find("receipt");
  uint64_t revision = 0;
  if (!wire::HasKeys(receipt, {"revision", "result"}) ||
      !wire::GetUnsigned(receipt, "revision", &revision).ok() ||
      revision != revision_ + 1 || !receipt.Find("result")->IsObject()) {
    return Freeze("journal revision mismatch");
  }
  revision_ = revision;
  reply_ =
      JournalReply{sequence_, revision, request_id_, *receipt.Find("result")};
  state_ = JournalState::kReady;
  return Status{};
}

Status JournalClient::Tick(int64_t now_ns) {
  if (state_ == JournalState::kNew || state_ == JournalState::kStopped) {
    return Status{};
  }
  if (now_ns < last_now_ns_) {
    if (state_ != JournalState::kStopping) {
      return Freeze("journal monotonic clock rollback");
    }
    now_ns = last_now_ns_;
    deadline_ns_ = now_ns;
  }
  last_now_ns_ = now_ns;
  if (!reaped_ && worker_.pidfd.Get() >= 0) {
    int code = 0;
    const auto status = ReapProcess(worker_.pidfd.Get(), &reaped_, &code);
    if (!status.ok()) return Freeze(status.message());
  }
  if (state_ == JournalState::kStopping) {
    if (reaped_) {
      state_ = JournalState::kStopped;
    } else if (now_ns >= deadline_ns_ && !kill_sent_) {
      SignalProcess(worker_.pidfd.Get(), SIGKILL);
      kill_sent_ = true;
      cleanup_deadline_ns_ =
          ValidTime(now_ns, 5'000'000'000) ? now_ns + 5'000'000'000 : now_ns;
    } else if (kill_sent_ && now_ns >= cleanup_deadline_ns_ &&
               failure_.empty()) {
      failure_ = "journal cleanup unconfirmed";
    }
    return Status{};
  }
  if (state_ == JournalState::kFrozen) return Status::Fail(failure_);
  if (reaped_) return Freeze("journal worker exited");
  if ((state_ == JournalState::kStarting || state_ == JournalState::kBusy) &&
      now_ns >= deadline_ns_)
    return Freeze("journal transaction timed out");
  return Receive();
}

void JournalClient::BeginStop(int64_t now_ns, int64_t grace_ns) {
  if (state_ == JournalState::kStopped || state_ == JournalState::kStopping) {
    return;
  }
  socket_.Reset();
  reply_.reset();
  state_ = JournalState::kStopping;
  // Stop is never rejected because the clock/arguments are bad. Kill promptly
  // in that case, retaining cleanup_unknown until the actual reap completes.
  const bool valid = ValidTime(now_ns, grace_ns) && now_ns >= last_now_ns_;
  deadline_ns_ = valid ? now_ns + grace_ns : last_now_ns_;
  if (valid) last_now_ns_ = now_ns;
  if (worker_.pidfd.Get() >= 0 && !reaped_) {
    SignalProcess(worker_.pidfd.Get(), valid ? SIGTERM : SIGKILL);
    kill_sent_ = !valid;
    if (!valid) cleanup_deadline_ns_ = last_now_ns_;
  } else if (worker_.identity.pid < 0) {
    reaped_ = true;
    state_ = JournalState::kStopped;
  }
}

std::optional<JournalReply> JournalClient::TakeReply() {
  auto result = std::move(reply_);
  reply_.reset();
  return result;
}
}  // namespace q4t::service
