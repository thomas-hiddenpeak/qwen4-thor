#include "q4t/server/state_key_admin.h"

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#include <atomic>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <future>

#include "q4t/server/output_schema.h"
#include "q4t/server/responses_execution.h"
#include "q4t/test.h"

extern char** environ;
namespace {
using namespace q4t::server;
using q4t::io::Json;
using q4t::service::OwnedFd;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
Json Parse(const std::string& raw) {
  Json result;
  if (!q4t::io::ParseJson(raw, &result, true))
    throw std::runtime_error("key admin fixture JSON");
  return result;
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = std::filesystem::absolute(
        output ? output : ".q4t-work/state-key-admin-cpu");
    std::filesystem::create_directories(root);
    auto name = (root / "key-admin-XXXXXX").string();
    if (!mkdtemp(name.data()))
      throw std::runtime_error("admin fixture directory");
    options.directory = name + "/objects";
    options.restart_backoff_ms = 0;
  }
  Json Request(const char* action = "inspect") const {
    return Parse(std::string(R"({"version":1,"action":")") + action + R"("})");
  }
  ObjectReply Call(Json request) const {
    return ExchangeStateKeyCommand(options.directory, request,
                                   Clock::now() + 3s);
  }
  Json Rotate(int64_t revision, std::string id = "") const {
    return Parse(R"({"version":1,"action":"rotate","expected_revision":)" +
                 std::to_string(revision) + R"(,"retire":[)" +
                 (id.empty() ? "" : '"' + id + '"') + "]}");
  }
};
StateAccess Access() { return {"client:cpu", Clock::now() + 3s, {}}; }
StateCipherBinding Binding() {
  return {"cpu", "template", "reasoning", "rs_1"};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
socklen_t Address(const Fixture& f, sockaddr_un* address) {
  const auto name = StateKeyAdminAddress(f.options.directory);
  *address = {};
  address->sun_family = AF_UNIX;
  std::memcpy(address->sun_path + 1, name.data(), name.size());
  return offsetof(sockaddr_un, sun_path) + 1 + name.size();
}
OwnedFd Connect(const Fixture& f) {
  sockaddr_un address{};
  const auto size = Address(f, &address);
  OwnedFd socket(::socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0));
  if (socket.Get() < 0 ||
      connect(socket.Get(), reinterpret_cast<sockaddr*>(&address), size) != 0)
    throw std::runtime_error("admin fixture connect");
  return socket;
}
struct Command {
  int exit = -1;
  std::string text;
};
Command Cli(std::vector<std::string> args) {
  args.insert(args.begin(), {"/proc/self/exe", "state-keys"});
  std::vector<char*> argv;
  for (auto& arg : args) argv.push_back(arg.data());
  argv.push_back(nullptr);
  int pipe_fds[2];
  if (pipe2(pipe_fds, O_CLOEXEC | O_NONBLOCK) != 0)
    throw std::runtime_error("admin CLI pipe");
  OwnedFd read(pipe_fds[0]), write(pipe_fds[1]);
  posix_spawn_file_actions_t actions;
  posix_spawn_file_actions_init(&actions);
  posix_spawn_file_actions_adddup2(&actions, write.Get(), STDOUT_FILENO);
  posix_spawn_file_actions_adddup2(&actions, write.Get(), STDERR_FILENO);
  posix_spawn_file_actions_addclose(&actions, read.Get());
  posix_spawn_file_actions_addclose(&actions, write.Get());
  pid_t child = -1;
  const int spawned =
      posix_spawn(&child, argv[0], &actions, nullptr, argv.data(), environ);
  posix_spawn_file_actions_destroy(&actions);
  if (spawned) throw std::runtime_error("admin CLI spawn");
  write.Reset();
  Command result;
  int status = 0;
  bool reaped = false;
  const auto deadline = Clock::now() + 8s;
  while (Clock::now() < deadline) {
    char buffer[4096];
    for (;;) {
      const auto size = ::read(read.Get(), buffer, sizeof(buffer));
      if (size <= 0) break;
      result.text.append(buffer, size);
    }
    if (reaped) {
      result.exit = WIFEXITED(status) ? WEXITSTATUS(status) : 128;
      return result;
    }
    reaped = waitpid(child, &status, WNOHANG) == child;
    pollfd descriptor{read.Get(), POLLIN, 0};
    poll(&descriptor, 1, 10);
  }
  if (!reaped) {
    kill(child, SIGKILL);  // This unreaped, directly spawned child only.
    waitpid(child, &status, 0);
  }
  throw std::runtime_error("admin CLI exceeded frozen CPU deadline");
}
}  // namespace

Q4T_TEST(state_key_admin_lazy_live_initialization_and_safe_inspection) {
  Fixture f;
  ObjectStore objects(f.options);
  auto admin = StateKeyAdmin::Start(objects, f.options.directory);
  Q4T_CHECK(admin && admin->Alive() && objects.Health().workers_started == 0);
  Q4T_CHECK(f.Call(f.Request()).http_status == 404);
  auto initialized = f.Call(f.Request("initialize"));
  Q4T_CHECK(Ok(initialized) && initialized.value.GetInt("revision") == 1);
  const auto observed = f.Call(f.Request());
  Q4T_CHECK(Ok(observed) &&
            OutputJson(initialized.value) == OutputJson(observed.value));
  Q4T_CHECK(OutputJson(observed.value).find("\"key\":") == std::string::npos);
  Q4T_CHECK(admin->Alive());
  return true;
}

Q4T_TEST(state_key_admin_online_rotation_retirement_and_state_continuity) {
  Fixture f;
  ObjectStore objects(f.options);
  auto admin = StateKeyAdmin::Start(objects, f.options.directory);
  Q4T_CHECK(admin);
  StateCipher cipher(objects);
  std::string old, current, plaintext;
  Q4T_CHECK(Ok(cipher.Seal("old", Binding(), Access(), &old)));
  auto observed = f.Call(f.Request());
  Q4T_CHECK(Ok(observed));
  const auto old_key = observed.value.GetString("active");
  Q4T_CHECK(Ok(f.Call(f.Rotate(observed.value.GetInt("revision")))));
  Q4T_CHECK(Ok(cipher.Open(old, Binding(), Access(), &plaintext)) &&
            plaintext == "old");
  Q4T_CHECK(Ok(cipher.Seal("new", Binding(), Access(), &current)));
  observed = f.Call(f.Request());
  Q4T_CHECK(Ok(f.Call(f.Rotate(observed.value.GetInt("revision"), old_key))));
  Q4T_CHECK(cipher.Open(old, Binding(), Access(), &plaintext).code ==
            "invalid_encrypted_content");
  Q4T_CHECK(Ok(cipher.Open(current, Binding(), Access(), &plaintext)) &&
            plaintext == "new");
  Q4T_CHECK(admin->Alive() && objects.Health().workers_started == 1);
  return true;
}

Q4T_TEST(state_key_admin_simultaneous_rotation_has_one_revision_winner) {
  Fixture f;
  ObjectStore objects(f.options);
  auto admin = StateKeyAdmin::Start(objects, f.options.directory);
  Q4T_CHECK(admin && Ok(f.Call(f.Request("initialize"))));
  auto one =
      std::async(std::launch::async, [&] { return f.Call(f.Rotate(1)); });
  auto two =
      std::async(std::launch::async, [&] { return f.Call(f.Rotate(1)); });
  const auto a = one.get(), b = two.get();
  Q4T_CHECK((a.ok() && b.http_status == 409) ||
            (b.ok() && a.http_status == 409));
  const auto observed = f.Call(f.Request());
  Q4T_CHECK(Ok(observed) && observed.value.GetInt("revision") == 2 &&
            observed.value.GetArray("keys")->array.size() == 2);
  return true;
}

Q4T_TEST(state_key_admin_rejects_malformed_duplicate_and_oversized_packets) {
  Fixture f;
  ObjectStore objects(f.options);
  auto admin = StateKeyAdmin::Start(objects, f.options.directory);
  Q4T_CHECK(admin);
  for (const auto& raw :
       {std::string("{"),
        std::string(R"({"version":1,"version":1,"action":"initialize"})"),
        std::string(R"({"version":1,"action":"initialize","key":"user key"})"),
        std::string(4097, 'x')}) {
    auto socket = Connect(f);
    Q4T_CHECK(send(socket.Get(), raw.data(), raw.size(), MSG_NOSIGNAL) ==
              static_cast<ssize_t>(raw.size()));
    pollfd descriptor{socket.Get(), POLLIN, 0};
    Q4T_CHECK(poll(&descriptor, 1, 1000) > 0);
    char bytes[16384];
    const auto size = recv(socket.Get(), bytes, sizeof(bytes), MSG_DONTWAIT);
    Q4T_CHECK(size > 0);
    const auto reply = Parse(std::string(bytes, size));
    Q4T_CHECK(reply.GetString("code") == "state_key_command_invalid");
  }
  Q4T_CHECK(objects.Health().workers_started == 0 && admin->Alive());
  return true;
}

Q4T_TEST(state_key_admin_idle_peer_does_not_block_another_operator) {
  Fixture f;
  ObjectStore objects(f.options);
  auto admin = StateKeyAdmin::Start(objects, f.options.directory);
  Q4T_CHECK(admin);
  auto idle = Connect(f);
  const auto started = Clock::now();
  Q4T_CHECK(Ok(f.Call(f.Request("initialize"))));
  Q4T_CHECK(Clock::now() - started < 1s);
  idle.Reset();
  Q4T_CHECK(Ok(f.Call(f.Request())));
  return true;
}

Q4T_TEST(state_key_admin_address_ownership_and_release_are_explicit) {
  Fixture f;
  ObjectStore objects(f.options);
  auto admin = StateKeyAdmin::Start(objects, f.options.directory);
  Q4T_CHECK(admin);
  Q4T_CHECK(!StateKeyAdmin::Start(objects, f.options.directory));
  Q4T_CHECK(StateKeyAdminAddress(f.options.directory + "//") ==
            StateKeyAdminAddress(f.options.directory));
  Q4T_CHECK(StateKeyAdminAddress("/").empty() &&
            StateKeyAdminAddress("../x").empty());
  Q4T_CHECK(Ok(f.Call(f.Request("initialize"))));
  admin.reset();
  const auto stopped = f.Call(f.Request());
  Q4T_CHECK(!stopped.ok() && !stopped.commit_unknown);
  admin = StateKeyAdmin::Start(objects, f.options.directory);
  Q4T_CHECK(admin && Ok(f.Call(f.Request())));
  return true;
}

Q4T_TEST(state_key_admin_actual_live_cli_and_invalid_options) {
  Fixture f;
  ObjectStore objects(f.options);
  auto admin = StateKeyAdmin::Start(objects, f.options.directory);
  Q4T_CHECK(admin);
  auto initialized =
      Cli({"initialize", "--object-store-dir", f.options.directory});
  Q4T_CHECK(initialized.exit == 0 &&
            Parse(initialized.text).GetInt("status") == 200);
  auto inspect = Cli({"inspect", "--object-store-dir", f.options.directory});
  Q4T_CHECK(inspect.exit == 0);
  const auto before = Parse(inspect.text);
  auto rotated = Cli({"rotate", "--object-store-dir", f.options.directory,
                      "--expected-revision", "1"});
  Q4T_CHECK(rotated.exit == 0 &&
            Parse(rotated.text).Find("value")->GetInt("revision") == 2);
  auto stale = Cli({"rotate", "--object-store-dir", f.options.directory,
                    "--expected-revision", "1"});
  Q4T_CHECK(stale.exit == 1 && Parse(stale.text).GetInt("status") == 409);
  Q4T_CHECK(Cli({"rotate", "--object-store-dir", f.options.directory}).exit ==
            2);
  Q4T_CHECK(Cli({"initialize", "--object-store-dir", "/", "--offline"}).exit ==
            2);
  const auto after = f.Call(f.Request());
  Q4T_CHECK(Ok(after) && after.value.GetInt("revision") == 2 &&
            before.Find("value")->GetString("active") !=
                after.value.GetString("active"));
  return true;
}

Q4T_TEST(state_key_admin_actual_offline_cli_obeys_exclusive_store_lock) {
  Fixture f;
  auto initialized = Cli(
      {"initialize", "--object-store-dir", f.options.directory, "--offline"});
  Q4T_CHECK(initialized.exit == 0);
  const auto first = Parse(initialized.text);
  auto offline =
      Cli({"inspect", "--object-store-dir", f.options.directory, "--offline"});
  Q4T_CHECK(offline.exit == 0 &&
            OutputJson(*Parse(offline.text).Find("value")) ==
                OutputJson(*first.Find("value")));
  ObjectStore objects(f.options);
  Q4T_CHECK(Ok(StateCipher(objects).Describe(Access())));
  auto blocked = Cli({"rotate", "--object-store-dir", f.options.directory,
                      "--expected-revision", "1", "--offline"});
  Q4T_CHECK(blocked.exit == 1);
  const auto current = StateCipher(objects).Describe(Access());
  Q4T_CHECK(Ok(current) && current.value.GetInt("revision") == 1 &&
            current.value.GetString("active") ==
                first.Find("value")->GetString("active"));
  return true;
}

Q4T_TEST(state_key_admin_lost_actual_rotation_receipt_is_not_replayed) {
  Fixture f;
  ObjectStore objects(f.options);
  StateCipher cipher(objects);
  Q4T_CHECK(Ok(cipher.Ready(Access())));
  sockaddr_un address{};
  const auto size = Address(f, &address);
  OwnedFd listener(socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0));
  Q4T_CHECK(listener.Get() >= 0 &&
            bind(listener.Get(), reinterpret_cast<sockaddr*>(&address), size) ==
                0 &&
            listen(listener.Get(), 1) == 0);
  auto worker = std::async(std::launch::async, [&] {
    pollfd ready{listener.Get(), POLLIN, 0};
    if (poll(&ready, 1, 2000) <= 0) return false;
    OwnedFd peer(accept4(listener.Get(), nullptr, nullptr, SOCK_CLOEXEC));
    if (peer.Get() < 0) return false;
    ready = {peer.Get(), POLLIN, 0};
    if (poll(&ready, 1, 2000) <= 0) return false;
    char bytes[4096];
    const auto received = recv(peer.Get(), bytes, sizeof(bytes), MSG_DONTWAIT);
    if (received <= 0) return false;
    return HandleStateKeyCommand(objects, Parse(std::string(bytes, received)),
                                 Access())
        .ok();
    // Peer closes after the actual commit, before sending any receipt.
  });
  const auto unknown = f.Call(f.Rotate(1));
  Q4T_CHECK(worker.get() && !unknown.ok() && unknown.commit_unknown);
  const auto after = cipher.Describe(Access());
  Q4T_CHECK(Ok(after) && after.value.GetInt("revision") == 2 &&
            after.value.GetArray("keys")->array.size() == 2);
  pollfd ready{listener.Get(), POLLIN, 0};
  Q4T_CHECK(poll(&ready, 1, 0) == 0);
  return true;
}

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
Q4T_TEST(state_key_admin_dependency_recovery_does_not_restart_core_or_channel) {
  Fixture f;
  ObjectStore objects(f.options);
  auto admin = StateKeyAdmin::Start(objects, f.options.directory);
  Q4T_CHECK(admin);
  struct Reset {
    ~Reset() { SetCryptoUnavailableForTesting(false); }
  } reset;
  SetCryptoUnavailableForTesting(true);
  Q4T_CHECK(f.Call(f.Request("initialize")).code == "state_crypto_unavailable");
  ChatPlanConfig config;
  config.model = "cpu";
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(Parse(R"({"input":"q","store":false})"), config,
                           Access(), 42)));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  ResponseDelta text;
  text.kind = ResponseDelta::Kind::kContent;
  text.text = "42";
  Q4T_CHECK(run.Append(text, "null", &wire));
  Q4T_CHECK(Ok(run.Finish(false, 0, {1, 0, 1, 0}, 43, &wire)));
  Q4T_CHECK(admin->Alive() && objects.Health().workers_started == 0);
  SetCryptoUnavailableForTesting(false);
  Q4T_CHECK(Ok(f.Call(f.Request("initialize"))) && admin->Alive());
  return true;
}

Q4T_TEST(state_key_admin_shutdown_cancels_waiting_io_without_worker_growth) {
  Fixture f;
  ObjectStore objects(f.options);
  auto admin = StateKeyAdmin::Start(objects, f.options.directory);
  Q4T_CHECK(admin);
  struct Reset {
    ~Reset() { DeferObjectSpawnForTesting(false); }
  } reset;
  DeferObjectSpawnForTesting(true);
  auto pending = std::async(std::launch::async,
                            [&] { return f.Call(f.Request("initialize")); });
  const auto until = Clock::now() + 1s;
  while (!objects.Health().pending && Clock::now() < until) poll(nullptr, 0, 5);
  Q4T_CHECK(objects.Health().pending > 0);
  const auto started = Clock::now();
  admin.reset();
  Q4T_CHECK(Clock::now() - started < 1s && !pending.get().ok());
  Q4T_CHECK(objects.Health().workers_started == 0);
  return true;
}
#endif
