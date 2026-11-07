#include "q4t/service/guardian_journal.h"
#include "q4t/service/guardian_wire.h"

#include <signal.h>
#include <unistd.h>

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <limits>
#include <thread>

#include "q4t/test.h"

namespace {
namespace s = q4t::service;
namespace w = q4t::service::wire;
using namespace std::chrono_literals;
constexpr char kService[] = "98765432-1234-5678-1234-567812345678";
std::string python, script, repository, output;
bool cleanup_ok = true;

int64_t Now() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

struct JournalFixture {
  JournalFixture() {
    std::string pattern = output + "/journal-XXXXXX";
    if (mkdtemp(pattern.data()) != nullptr) directory = pattern;
  }
  ~JournalFixture() {
    client.BeginStop(Now() + offset, 0);
    const int64_t end = Now() + 3'000'000'000;
    while (!client.CleanupConfirmed() && Now() < end) {
      client.Tick(Now() + offset);
      std::this_thread::sleep_for(2ms);
    }
    cleanup_ok = cleanup_ok && client.CleanupConfirmed();
    if (client.CleanupConfirmed() && !directory.empty()) {
      std::filesystem::remove_all(directory);
    }
  }
  bool Initialize() {
    if (directory.empty()) return false;
    const std::string code =
        "import sys;sys.path.insert(0,sys.argv[3]);"
        "from tools.serve_recovery.store import Store;"
        "s=Store(sys.argv[1],sys.argv[2],initialize=True);s.close()";
    s::ChildProcess init;
    if (!s::SpawnProcess(
             {python, "-I", "-B", "-c", code, directory, kService, repository},
             {}, {}, &init)
             .ok())
      return false;
    const int64_t end = Now() + 3'000'000'000;
    while (Now() < end) {
      bool reaped = false;
      int status = 0;
      if (!s::ReapProcess(init.pidfd.Get(), &reaped, &status).ok())
        return false;
      if (reaped) return status == 0;
      std::this_thread::sleep_for(2ms);
    }
    s::SignalProcess(init.pidfd.Get(), SIGKILL);
    cleanup_ok = false;
    return false;
  }
  bool Wait(s::JournalState state) {
    const int64_t end = Now() + 3'000'000'000;
    while (Now() < end) {
      const auto status = client.Tick(Now() + offset);
      if (client.State() == state) return true;
      if (!status.ok() && state != s::JournalState::kStopped) return false;
      std::this_thread::sleep_for(2ms);
    }
    return false;
  }
  bool Start() {
    return Initialize() &&
           client
               .Start(python, script, directory, kService, Now() + offset,
                      3'000'000'000)
               .ok() &&
           Wait(s::JournalState::kReady);
  }
  std::optional<s::JournalReply> Send(const std::string& operation,
                                      const q4t::io::Json& args) {
    uint64_t sequence = 0;
    if (!client.Submit(operation, args, Now() + offset, &sequence).ok() ||
        !Wait(s::JournalState::kReady))
      return std::nullopt;
    return client.TakeReply();
  }
  bool Pause() {
    if (!s::SignalProcess(client.WorkerPidfd(), SIGSTOP).ok()) return false;
    const int64_t end = Now() + 3'000'000'000;
    while (Now() < end) {
      s::ProcessRecord record;
      if (!s::ReadProcess(client.Worker().pid, &record).ok()) return false;
      if (record.state == 'T') return true;
      std::this_thread::sleep_for(2ms);
    }
    return false;
  }
  s::JournalClient client;
  std::string directory;
  int64_t offset = 0;
};

q4t::io::Json Proof() {
  return w::Object({{"cleanup_complete", w::Boolean(true)},
                    {"resources_safe", w::Boolean(true)},
                    {"identity_valid", w::Boolean(true)},
                    {"lease_continuous", w::Boolean(true)},
                    {"device_usable", w::Boolean(true)}});
}
}  // namespace

Q4T_TEST(guardian_wire_preserves_64_bit_ledger_integer_without_rounding) {
  constexpr uint64_t number = std::numeric_limits<int64_t>::max();
  std::string encoded;
  Q4T_CHECK(w::Encode(w::Object({{"revision", w::Unsigned(number)},
                                 {"message",
                                  w::String("quote\" newline\n slash\\")}}),
                      &encoded)
                .ok());
  q4t::io::Json parsed;
  Q4T_CHECK(w::Parse(encoded, &parsed).ok());
  uint64_t observed = 0;
  Q4T_CHECK(w::GetUnsigned(parsed, "revision", &observed).ok());
  Q4T_CHECK(observed == number);
  Q4T_CHECK(parsed.GetString("message") == "quote\" newline\n slash\\");
  return true;
}

Q4T_TEST(guardian_wire_rejects_duplicates_and_nonintegral_revisions) {
  q4t::io::Json parsed;
  Q4T_CHECK(!w::Parse("{\"revision\":1,\"revision\":2}", &parsed).ok());
  Q4T_CHECK(!w::Parse(std::string(w::kMaxFrame + 1, ' '), &parsed).ok());
  for (const auto* value :
       {"true", "1.0", "1e0", "-1", "9223372036854775808"}) {
    Q4T_CHECK(
        w::Parse(std::string("{\"revision\":") + value + "}", &parsed).ok());
    uint64_t ignored = 0;
    Q4T_CHECK(!w::GetUnsigned(parsed, "revision", &ignored).ok());
  }
  auto injection = w::Unsigned(1);
  injection.str = "1,\"extra\":true";
  std::string encoded;
  Q4T_CHECK(!w::Encode(w::Object({{"revision", injection}}), &encoded).ok());
  return true;
}

Q4T_TEST(guardian_journal_real_worker_handshake_transaction_and_cleanup) {
  JournalFixture f;
  Q4T_CHECK(f.Start());
  Q4T_CHECK(f.client.Snapshot().GetString("desired") == "STOPPED");
  const auto reply =
      f.Send("desired", w::Object({{"state", w::String("RUNNING")}}));
  Q4T_CHECK(reply && reply->revision == 1 && reply->sequence == 1);
  Q4T_CHECK(reply->result.GetString("desired") == "RUNNING");
  const auto stop =
      f.Send("desired", w::Object({{"state", w::String("STOPPED")}}));
  Q4T_CHECK(stop && stop->revision == 2 && stop->sequence == 2);
  f.client.BeginStop(Now(), 0);
  Q4T_CHECK(f.Wait(s::JournalState::kStopped));
  Q4T_CHECK(f.client.CleanupConfirmed());
  return true;
}

Q4T_TEST(guardian_journal_allows_one_pending_transaction_and_receipt) {
  JournalFixture f;
  Q4T_CHECK(f.Start());
  Q4T_CHECK(f.Pause());
  uint64_t sequence = 0, second = 0;
  const auto args = w::Object({{"state", w::String("RUNNING")}});
  Q4T_CHECK(f.client.Submit("desired", args, Now(), &sequence).ok());
  Q4T_CHECK(!f.client.Submit("desired", args, Now(), &second).ok());
  Q4T_CHECK(s::SignalProcess(f.client.WorkerPidfd(), SIGCONT).ok());
  Q4T_CHECK(f.Wait(s::JournalState::kReady));
  Q4T_CHECK(!f.client.Submit("desired", args, Now(), &second).ok());
  const auto reply = f.client.TakeReply();
  Q4T_CHECK(reply && reply->sequence == sequence && reply->revision == 1);
  Q4T_CHECK(!f.client.TakeReply());
  return true;
}

Q4T_TEST(guardian_journal_timeout_freezes_without_blocking_or_spawning_worker) {
  JournalFixture f;
  Q4T_CHECK(f.Start());
  const auto owner = f.client.Worker();
  Q4T_CHECK(f.Pause());
  uint64_t sequence = 0;
  Q4T_CHECK(f.client
                .Submit("desired", w::Object({{"state", w::String("RUNNING")}}),
                        Now(), &sequence)
                .ok());
  f.offset = 4'000'000'000;
  Q4T_CHECK(!f.client.Tick(Now() + f.offset).ok());
  Q4T_CHECK(f.client.State() == s::JournalState::kFrozen);
  Q4T_CHECK(f.client.Worker() == owner && !f.client.CleanupConfirmed());
  Q4T_CHECK(
      !f.client
           .Start(python, script, f.directory, kService, Now(), 3'000'000'000)
           .ok());
  Q4T_CHECK(!f.client.TakeReply());
  f.client.BeginStop(Now() + f.offset, 0);
  Q4T_CHECK(f.Wait(s::JournalState::kStopped));
  Q4T_CHECK(f.client.CleanupConfirmed());
  return true;
}

Q4T_TEST(guardian_stop_rejects_late_real_durable_authorization) {
  JournalFixture f;
  Q4T_CHECK(f.Start());
  Q4T_CHECK(f.Send("desired", w::Object({{"state", w::String("RUNNING")}})));
  Q4T_CHECK(f.Pause());
  uint64_t sequence = 0;
  Q4T_CHECK(f.client
                .Submit("authorize",
                        w::Object({{"role", w::String("controller")},
                                   {"proof", Proof()}}),
                        Now(), &sequence)
                .ok());
  s::LaunchGate launch;
  Q4T_CHECK(launch.Begin(sequence, 1));
  launch.Stop();  // Remains responsive while the real journal is SIGSTOPed.
  Q4T_CHECK(s::SignalProcess(f.client.WorkerPidfd(), SIGCONT).ok());
  Q4T_CHECK(f.Wait(s::JournalState::kReady));
  const auto reply = f.client.TakeReply();
  Q4T_CHECK(reply && reply->result.GetBool("allowed"));
  Q4T_CHECK(!launch.Consume(reply->sequence, 1));
  Q4T_CHECK(f.Send("desired", w::Object({{"state", w::String("STOPPED")}})));
  return true;
}

Q4T_TEST(guardian_dead_journal_discards_unconsumed_launch_permission) {
  JournalFixture f;
  Q4T_CHECK(f.Start());
  Q4T_CHECK(f.Send("desired", w::Object({{"state", w::String("RUNNING")}})));
  uint64_t sequence = 0;
  Q4T_CHECK(f.client
                .Submit("authorize",
                        w::Object({{"role", w::String("controller")},
                                   {"proof", Proof()}}),
                        Now(), &sequence)
                .ok());
  Q4T_CHECK(f.Wait(s::JournalState::kReady));
  Q4T_CHECK(s::SignalProcess(f.client.WorkerPidfd(), SIGKILL).ok());
  const int64_t end = Now() + 3'000'000'000;
  bool exited = false;
  while (Now() < end && !exited) {
    Q4T_CHECK(s::ProcessExited(f.client.WorkerPidfd(), &exited).ok());
    if (!exited) std::this_thread::sleep_for(2ms);
  }
  Q4T_CHECK(exited);
  Q4T_CHECK(!f.client.Tick(Now()).ok());
  Q4T_CHECK(f.client.State() == s::JournalState::kFrozen);
  Q4T_CHECK(!f.client.TakeReply());
  Q4T_CHECK(f.client.CleanupConfirmed());
  return true;
}

int main(int argc, char** argv) {
  if (argc != 3 || !s::EnableSubreaper().ok()) return 2;
  python = argv[1];
  script = std::filesystem::canonical(argv[2]).string();
  repository = std::filesystem::path(script)
                   .parent_path()
                   .parent_path()
                   .parent_path()
                   .string();
  const char* directory = std::getenv("Q4T_TEST_OUTPUT_DIR");
  if (directory == nullptr) return 2;
  output = directory;
  std::filesystem::create_directories(output);
  std::vector<std::string> names;
  for (const auto& test : q4t::test::Registry()) names.push_back(test.name);
  const int result = q4t::test::RunTests(names, false);
  bool empty = false;
  return cleanup_ok && s::NoChildren(&empty).ok() && empty ? result : 1;
}
