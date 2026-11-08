#include "q4t/server/progress_context.h"
#include "q4t/server/progress_transport.h"
#include "q4t/server/request_diagnostics.h"

#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <condition_variable>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <optional>
#include <stdexcept>
#include <thread>

#include "q4t/server/lifetime.h"
#include "q4t/server/responses_owner.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::service::OwnedFd;
using Clock = std::chrono::steady_clock;
uint64_t Number(const ProgressPacket& packet, size_t field) {
  uint64_t result = 0;
  for (int i = 7; i >= 0; --i)
    result = (result << 8) | packet.data[8 + field * 8 + i];
  return result;
}
ProgressIdentity Identity() {
  const auto current = CurrentResponseOwner();
  ProgressIdentity identity;
  identity.pid = current.GetInt("pid");
  identity.start_ticks = current.GetInt("start_ticks");
  identity.pid_namespace = current.GetInt("pid_namespace");
  const auto boot = current.GetString("boot_id");
  std::copy_n(boot.data(), 36, identity.boot_id.data());
  const std::string service = "00000000-0000-4000-8000-000000000001";
  std::copy_n(service.data(), 36, identity.service_id.data());
  identity.controller_generation = 3;
  identity.model_generation = 7;
  return identity;
}
std::array<OwnedFd, 2> Sockets() {
  int pair[2];
  if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0,
                 pair))
    throw std::runtime_error("test progress pair");
  return {OwnedFd(pair[0]), OwnedFd(pair[1])};
}
}  // namespace

Q4T_TEST(progress_context_nested_scope_does_not_fake_completion) {
  RequestProgress progress;
  auto lease = progress.Acquire(ProgressNowNs() + 1000000000);
  ProgressContext context(lease.Handle());
  {
    ProgressExecution prefill(ProgressWork::kPrefill);
    {
      ProgressExecution helper(ProgressWork::kDraft);
      ProgressExecution::Completed();
    }
    auto snapshot = progress.Read();
    Q4T_CHECK(snapshot.complete && snapshot.rows[0].work_id == 1 &&
              snapshot.rows[0].work == ProgressWork::kPrefill &&
              snapshot.rows[0].completed == 1);
  }
  ProgressExecution::Completed();  // No active work cannot count a heartbeat.
  auto snapshot = progress.Read();
  Q4T_CHECK(snapshot.complete && snapshot.rows[0].completed == 1 &&
            snapshot.rows[0].work == ProgressWork::kNone);
  auto other = progress.Acquire(ProgressNowNs() + 1000000000);
  {
    ProgressExecution outer(ProgressWork::kDecode);
    {
      ProgressContext wrong_owner(other.Handle());
      ProgressExecution unsupported_nesting(ProgressWork::kDecode);
      ProgressExecution::Completed();
    }
  }
  snapshot = progress.Read();
  Q4T_CHECK(!snapshot.complete && snapshot.rows[0].completed == 1);
  return true;
}

Q4T_TEST(progress_context_batch_counts_only_selected_owners) {
  RequestProgress progress;
  auto a = progress.Acquire(10000000000000000);
  auto b = progress.Acquire(10000000000000000);
  auto waiting = progress.Acquire(10000000000000000);
  const std::array selected{a.Handle(), b.Handle()};
  ProgressBatch batch(selected, [](auto handle) { return handle; });
  {
    ProgressContext context(batch.Handles());
    ProgressExecution execution(ProgressWork::kDecode);
    ProgressExecution::Completed();
  }
  const auto snapshot = progress.Read();
  Q4T_CHECK(snapshot.complete && snapshot.count == 3);
  Q4T_CHECK(snapshot.rows[0].completed == 1 &&
            snapshot.rows[1].completed == 1 &&
            snapshot.rows[2].completed == 0 && snapshot.rows[2].work_id == 0);
  return true;
}

Q4T_TEST(progress_activity_nested_completion_preserves_outer_work) {
  using Kind = q4t::runtime::ExecutionActivityKind;
  using Activity = q4t::runtime::ExecutionActivity;
  RequestProgress progress;
  auto lease = progress.Acquire(ProgressNowNs() + 1000000000);
  lease.Handle().Stage(ProgressStage::kMedia);
  ProgressContext context(lease.Handle());
  ProgressExecution execution(ProgressWork::kMedia);
  {
    Activity decode(Kind::kMediaDecode);
    Q4T_CHECK(progress.Read().rows[0].stage == ProgressStage::kMediaDecode);
    {
      Activity read(Kind::kPleRead);
      Q4T_CHECK(progress.Read().rows[0].stage == ProgressStage::kPleRead);
      read.Completed();
    }
    Q4T_CHECK(progress.Read().rows[0].stage == ProgressStage::kMediaDecode);
    decode.Finish();
    decode.Finish();  // Ended scopes cannot count twice.
  }
  const auto value = progress.Read();
  Q4T_CHECK(value.complete && value.rows[0].completed == 2 &&
            value.rows[0].stage == ProgressStage::kMedia &&
            value.rows[0].work == ProgressWork::kMedia &&
            value.rows[0].work_id == 1);
  return true;
}

Q4T_TEST(progress_activity_failure_and_unrelated_thread_do_not_complete) {
  using Kind = q4t::runtime::ExecutionActivityKind;
  using Activity = q4t::runtime::ExecutionActivity;
  RequestProgress progress;
  auto lease = progress.Acquire(ProgressNowNs() + 1000000000);
  lease.Handle().Stage(ProgressStage::kPrefill);
  ProgressContext context(lease.Handle());
  ProgressExecution execution(ProgressWork::kPrefill);
  try {
    Activity read(Kind::kPleRead);
    throw std::runtime_error("CPU read failure");
  } catch (const std::runtime_error&) {
  }
  std::thread unrelated([] {
    Activity work(Kind::kPleRead);
    work.Finish();
  });
  unrelated.join();
  const auto value = progress.Read();
  Q4T_CHECK(value.complete && value.rows[0].completed == 0 &&
            value.rows[0].stage == ProgressStage::kPrefill);
  return true;
}

Q4T_TEST(progress_activity_depth_exhaustion_is_unknown_without_waiting) {
  using Kind = q4t::runtime::ExecutionActivityKind;
  using Activity = q4t::runtime::ExecutionActivity;
  RequestProgress progress;
  auto lease = progress.Acquire(ProgressNowNs() + 1000000000);
  ProgressContext context(lease.Handle());
  ProgressExecution execution(ProgressWork::kPrefill);
  std::array<std::optional<Activity>, 9> scopes;
  for (auto& scope : scopes) scope.emplace(Kind::kPleRead);
  Q4T_CHECK(!progress.Read().complete);
  for (size_t i = scopes.size(); i-- > 0;) scopes[i].reset();
  Q4T_CHECK(!progress.Read().complete);
  return true;
}

Q4T_TEST(progress_activity_captured_owner_cannot_complete_another_context) {
  using Kind = q4t::runtime::ExecutionActivityKind;
  using Activity = q4t::runtime::ExecutionActivity;
  RequestProgress progress;
  auto a = progress.Acquire(ProgressNowNs() + 1000000000);
  auto b = progress.Acquire(ProgressNowNs() + 1000000000);
  ProgressContext context(a.Handle());
  ProgressExecution execution(ProgressWork::kPrefill);
  Activity read(Kind::kPleRead);
  {
    ProgressContext other(b.Handle());
    read.Completed();
  }
  const auto value = progress.Read();
  Q4T_CHECK(!value.complete && value.count == 1 &&
            value.rows[0].ticket == b.Handle().ticket() &&
            value.rows[0].completed == 0);
  return true;
}

Q4T_TEST(progress_service_loop_counts_own_cycle_not_publisher_heartbeats) {
  RequestProgress progress;
  auto loop = progress.Acquire(0);
  loop.Handle().Stage(ProgressStage::kControl);
  ProgressContext context(loop.Handle());
  {
    ProgressExecution cycle(ProgressWork::kServiceLoop);
    for (uint64_t sequence = 1; sequence <= 3; ++sequence)
      Q4T_CHECK(EncodeProgress(progress.Read(), Identity(), sequence, 0, 0)
                    .size == 280);
    Q4T_CHECK(progress.Read().rows[0].completed == 0);
    ProgressExecution::Completed();
  }
  {
    ProgressExecution next(ProgressWork::kServiceLoop);
    const auto value = progress.Read();
    Q4T_CHECK(value.complete && value.rows[0].completed == 1 &&
              value.rows[0].work_id == 2 && value.rows[0].deadline_ns == 0 &&
              value.rows[0].stage == ProgressStage::kControl &&
              value.rows[0].work == ProgressWork::kServiceLoop);
  }
  return true;
}

Q4T_TEST(progress_diagnostic_nested_wait_restores_real_phase) {
  RequestProgress progress;
  auto lease = progress.Acquire(10000000000000000);
  FILE* sink = std::tmpfile();
  Q4T_CHECK(sink);
  ScopeExit close([&] { std::fclose(sink); });
  RequestDiagnostics diagnostic(sink);
  diagnostic.BindProgress(lease.Handle());
  diagnostic.Stage("decode");
  {
    RequestDiagnostics::Scope wait(diagnostic,
                                   RequestDiagnostics::Wait::kModel);
    Q4T_CHECK(progress.Read().rows[0].wait == ProgressWait::kModel);
    {
      RequestDiagnostics::Scope inner(diagnostic,
                                      RequestDiagnostics::Wait::kStorage);
      Q4T_CHECK(progress.Read().rows[0].wait == ProgressWait::kStorage);
    }
    Q4T_CHECK(progress.Read().rows[0].wait == ProgressWait::kModel);
  }
  const auto snapshot = progress.Read();
  Q4T_CHECK(snapshot.complete && snapshot.rows[0].completed == 0 &&
            snapshot.rows[0].wait == ProgressWait::kNone &&
            snapshot.rows[0].stage == ProgressStage::kDecode);
  return true;
}

Q4T_TEST(progress_scheduler_handoff_keeps_one_writer) {
  RequestProgress progress;
  auto lease = progress.Acquire(10000000000000000);
  RequestControl request("cpu", "private", Clock::time_point::max());
  request.BindProgress(lease.Handle());
  FILE* sink = std::tmpfile();
  Q4T_CHECK(sink);
  ScopeExit close([&] { std::fclose(sink); });
  RequestDiagnostics diagnostic(sink);
  diagnostic.BindProgress(lease.Handle());
  std::mutex mutex;
  std::condition_variable cv;
  bool pending = false, done = false, stop = false;
  std::thread scheduler([&] {
    std::unique_lock lock(mutex);
    for (;;) {
      cv.wait(lock, [&] { return pending || stop; });
      if (stop) return;
      lock.unlock();
      {
        ProgressContext context(request.Progress());
        ProgressExecution execution(ProgressWork::kDecode);
        ProgressExecution::Completed();
      }
      lock.lock();
      pending = false;
      done = true;
      cv.notify_all();
    }
  });
  for (int i = 0; i < 16; ++i) {
    RequestDiagnostics::Scope waiting(diagnostic,
                                      RequestDiagnostics::Wait::kScheduler);
    std::unique_lock lock(mutex);
    done = false;
    pending = true;
    cv.notify_all();
    cv.wait(lock, [&] { return done; });
  }
  {
    std::lock_guard lock(mutex);
    stop = true;
  }
  cv.notify_all();
  scheduler.join();
  const auto snapshot = progress.Read();
  Q4T_CHECK(snapshot.complete && snapshot.rows[0].completed == 16 &&
            snapshot.rows[0].wait == ProgressWait::kNone &&
            snapshot.rows[0].work == ProgressWork::kNone);
  return true;
}

Q4T_TEST(progress_packet_identity_and_exact_wire) {
  RequestProgress progress;
  auto lease = progress.Acquire(900, 1);
  Q4T_CHECK(lease.Handle().Stage(ProgressStage::kDecode, 2));
  Q4T_CHECK(lease.Handle().Begin(ProgressWork::kDecode, 3));
  Q4T_CHECK(lease.Handle().Completed(4));
  const auto identity = Identity();
  const auto packet = EncodeProgress(progress.Read(5), identity, 9, 2, 7);
  Q4T_CHECK(identity.Valid() && packet.size == 280);
  Q4T_CHECK(std::string(reinterpret_cast<const char*>(packet.data.data()), 8) ==
            "Q4TPR002");
  Q4T_CHECK(Number(packet, 0) == 9 && Number(packet, 1) == 5 &&
            Number(packet, 2) == 1 && Number(packet, 3) == uint64_t(getpid()) &&
            Number(packet, 4) == identity.start_ticks &&
            Number(packet, 6) == 3 && Number(packet, 7) == 7 &&
            Number(packet, 10) == 1 && Number(packet, 11) == 2 &&
            Number(packet, 12) == 7);
  // Preserve an actual C++ frame for the Python decoder's subsequent checks.
  const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
  const auto directory =
      std::filesystem::path(output ? output : ".q4t-work/progress-cpu");
  std::filesystem::create_directories(directory);
  std::ofstream file(directory / "native-progress.bin", std::ios::binary);
  file.write(reinterpret_cast<const char*>(packet.data.data()), packet.size);
  file.close();
  Q4T_CHECK(file.good());
  return true;
}

Q4T_TEST(progress_packet_bounds_and_unknown_rows) {
  RequestProgress::Snapshot snapshot;
  snapshot.observed_ns = 10;
  snapshot.complete = true;
  snapshot.count = RequestProgress::kCapacity;
  snapshot.active = snapshot.count;
  auto identity = Identity();
  auto packet = EncodeProgress(snapshot, identity, 1, 0, 0);
  Q4T_CHECK(packet.size == packet.data.size() && packet.size < 100000);
  snapshot.count += 1;
  packet = EncodeProgress(snapshot, identity, 2, 0, 0);
  Q4T_CHECK(packet.size == packet.data.size() && Number(packet, 2) == 0);
  snapshot.count = 0;
  identity.model_generation = 0;
  packet = EncodeProgress(snapshot, identity, 3, 0, 0);
  Q4T_CHECK(Number(packet, 2) == 0);
  identity = Identity();
  snapshot.complete = false;
  packet = EncodeProgress(snapshot, identity, 4, 0, 0);
  Q4T_CHECK(Number(packet, 2) == 0);
  return true;
}

Q4T_TEST(progress_publisher_backpressure_is_nonblocking) {
  RequestProgress progress;
  auto lease = progress.Acquire(10000000000000000);
  std::atomic<uint64_t> successes{0}, steps{0};
  auto pair = Sockets();
  int buffer = 4096;
  Q4T_CHECK(setsockopt(pair[1].Get(), SOL_SOCKET, SO_SNDBUF, &buffer,
                       sizeof(buffer)) == 0);
  ProgressPublisher publisher(std::move(pair[1]), Identity(), progress,
                              successes, steps);
  bool filled = false;
  for (int i = 0; i < 128; ++i)
    if (!publisher.Publish()) filled = true;
  Q4T_CHECK(filled && publisher.Dropped() > 0);
  Q4T_CHECK(lease.Handle().Begin(ProgressWork::kDecode));
  Q4T_CHECK(lease.Handle().Completed());
  Q4T_CHECK(lease.Handle().End());
  Q4T_CHECK(progress.Read().rows[0].completed == 1);
  pair[0] = OwnedFd();
  Q4T_CHECK(!publisher.Publish());  // EPIPE cannot deliver SIGPIPE to business.
  Q4T_CHECK(progress.Read().complete);
  return true;
}

Q4T_TEST(progress_publisher_stop_and_invalid_fd_are_isolated) {
  RequestProgress progress;
  std::atomic<uint64_t> successes{0}, steps{0};
  auto pair = Sockets();
  {
    ProgressPublisher publisher(std::move(pair[1]), Identity(), progress,
                                successes, steps);
    publisher.Start();
    pollfd readable{pair[0].Get(), POLLIN, 0};
    Q4T_CHECK(poll(&readable, 1, 1000) == 1 && (readable.revents & POLLIN));
  }
  const int wrong = open("/dev/null", O_RDWR | O_CLOEXEC);
  Q4T_CHECK(wrong >= 0);
  bool rejected = false;
  try {
    ProgressPublisher publisher(OwnedFd(wrong), Identity(), progress, successes,
                                steps);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  Q4T_CHECK(rejected && fcntl(wrong, F_GETFD) < 0);
  const char* prior = std::getenv("Q4T_PROGRESS_FD");
  const std::string saved = prior ? prior : "";
  ScopeExit restore([&] {
    if (prior)
      setenv("Q4T_PROGRESS_FD", saved.c_str(), 1);
    else
      unsetenv("Q4T_PROGRESS_FD");
  });
  setenv("Q4T_PROGRESS_FD", "1", 1);
  Q4T_CHECK(!StartProgressPublisher(progress, successes, steps));
  Q4T_CHECK(fcntl(STDOUT_FILENO, F_GETFD) >= 0 && progress.Read().complete);
  return true;
}
