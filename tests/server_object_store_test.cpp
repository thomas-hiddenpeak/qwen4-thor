#include "q4t/server/object_store.h"

#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <future>
#include <thread>

#include "q4t/server/grammar_process.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
using q4t::io::Json;
namespace fs = std::filesystem;
Json Parse(const std::string& text) {
  Json result;
  if (!q4t::io::ParseJson(text, &result, true))
    throw std::runtime_error("invalid fixture JSON");
  return result;
}
struct Fixture {
  fs::path directory;
  ObjectStoreOptions options;
  Fixture() {
    const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = fs::absolute(output ? output : ".q4t-work/object-store-host");
    fs::create_directories(root);
    auto name = (root / "objects-XXXXXX").string();
    if (!mkdtemp(name.data())) throw std::runtime_error("fixture directory");
    directory = name;
    options.directory = (directory / "store").string();
    options.restart_backoff_ms = 0;
  }
  std::string Record() const {
    for (const auto& entry : fs::directory_iterator(options.directory))
      if (entry.path().extension() == ".json") return entry.path().string();
    throw std::runtime_error("no stored record");
  }
};
ObjectRequest Request(ObjectOperation op = ObjectOperation::kPut,
                      std::string id = "resp_1",
                      std::string scope = "client:a") {
  ObjectRequest request;
  request.operation = op;
  request.id = std::move(id);
  request.scope = std::move(scope);
  request.data = Parse(R"({"answer":"资料✅","exact":9007199254740993})");
  return request;
}
ObjectReply Run(ObjectStore& store, const ObjectRequest& request) {
  return store.Execute(request, Clock::now() + 5s);
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
bool GrammarContinues() {
  std::unique_ptr<q4t::server::detail::GrammarProcess> process;
  if (!q4t::server::detail::GrammarProcess::Start("--grammar-fixture-short", {},
                                                  &process))
    return false;
  char byte;
  return process->Transfer(&byte, 1, false, Clock::now() + 2s) && byte == 'x';
}
struct Injection {
  explicit Injection(std::string entry = {}) {
    ObjectWorkerEntryForTesting(std::move(entry));
  }
  ~Injection() {
    ObjectWorkerEntryForTesting({});
    DeferObjectSpawnForTesting(false);
  }
};
}  // namespace

Q4T_TEST(object_store_durable_crud_and_exact_data) {
  Fixture f;
  {
    ObjectStore store(f.options);
    auto write = Run(store, Request());
    Q4T_CHECK(Ok(write) && write.value.GetInt("revision") == 1);
    auto read = Run(store, Request(ObjectOperation::kGet));
    Q4T_CHECK(Ok(read) && read.value.Find("data") &&
              OutputJson(*read.value.Find("data")).find("9007199254740993") !=
                  std::string::npos);
    Q4T_CHECK(!read.value.Has("owner") && !read.value.Has("format"));
    Q4T_CHECK(store.Health().workers_started == 1 &&
              store.Health().state == "ready");
  }
  ObjectStore restarted(f.options);
  auto read = Run(restarted, Request(ObjectOperation::kGet));
  Q4T_CHECK(Ok(read) && read.value.GetInt("revision") == 1);
  auto erase = Request(ObjectOperation::kErase);
  erase.expected_revision = 1;
  Q4T_CHECK(Ok(Run(restarted, erase)));
  Q4T_CHECK(Run(restarted, Request(ObjectOperation::kGet)).http_status == 404);
  return true;
}

Q4T_TEST(object_store_revision_conflicts_prevent_lost_updates) {
  Fixture f;
  ObjectStore store(f.options);
  Q4T_CHECK(Ok(Run(store, Request())));
  Q4T_CHECK(Run(store, Request()).http_status == 409);
  auto update = Request();
  update.expected_revision = 1;
  update.data = Parse(R"({"v":2})");
  auto one = std::async(std::launch::async, [&] { return Run(store, update); });
  auto two = std::async(std::launch::async, [&] { return Run(store, update); });
  const auto a = one.get(), b = two.get();
  Q4T_CHECK((a.ok() && b.http_status == 409) ||
            (b.ok() && a.http_status == 409));
  auto read = Run(store, Request(ObjectOperation::kGet));
  Q4T_CHECK(Ok(read) && read.value.GetInt("revision") == 2);
  auto erase = Request(ObjectOperation::kErase);
  erase.expected_revision = 1;
  Q4T_CHECK(Run(store, erase).http_status == 409);
  return true;
}

Q4T_TEST(object_store_namespace_and_kind_isolation) {
  Fixture f;
  ObjectStore store(f.options);
  Q4T_CHECK(Ok(Run(store, Request())));
  Q4T_CHECK(Run(store, Request(ObjectOperation::kGet, "resp_1", "client:b"))
                .http_status == 404);
  Q4T_CHECK(Run(store, Request(ObjectOperation::kErase, "resp_1", "anonymous"))
                .http_status == 404);
  auto chat = Request(ObjectOperation::kGet);
  chat.kind = ObjectKind::kChat;
  Q4T_CHECK(Run(store, chat).http_status == 404);
  auto list = Request(ObjectOperation::kList, "", "client:b");
  auto empty = Run(store, list);
  Q4T_CHECK(Ok(empty) && empty.value.GetArray("data")->array.empty());
  list.after = "resp_1";
  Q4T_CHECK(Run(store, list).code == "invalid_object_cursor");
  Q4T_CHECK(
      Ok(Run(store, Request(ObjectOperation::kPut, "resp_1", "client:b"))));
  return true;
}

Q4T_TEST(object_store_list_order_and_bidirectional_cursors) {
  Fixture f;
  ObjectStore store(f.options);
  for (int i = 1; i <= 4; ++i)
    Q4T_CHECK(Ok(Run(
        store, Request(ObjectOperation::kPut, "resp_" + std::to_string(i)))));
  auto query = Request(ObjectOperation::kList);
  query.limit = 2;
  auto first = Run(store, query);
  Q4T_CHECK(Ok(first) && first.value.GetString("first_id") == "resp_4" &&
            first.value.GetString("last_id") == "resp_3" &&
            first.value.GetBool("has_more"));
  query.after = "resp_3";
  auto second = Run(store, query);
  Q4T_CHECK(Ok(second) && second.value.GetString("first_id") == "resp_2" &&
            second.value.GetString("last_id") == "resp_1" &&
            !second.value.GetBool("has_more"));
  query.after.clear();
  query.before = "resp_2";
  auto before = Run(store, query);
  Q4T_CHECK(Ok(before) && before.value.GetString("first_id") == "resp_4");
  query.before.clear();
  query.descending = false;
  Q4T_CHECK(Run(store, query).value.GetString("first_id") == "resp_1");
  return true;
}

Q4T_TEST(object_store_quotas_and_delete_reclaim) {
  Fixture f;
  f.options.max_client_objects = 1;
  f.options.max_total_objects = 2;
  ObjectStore store(f.options);
  Q4T_CHECK(Ok(Run(store, Request())));
  Q4T_CHECK(Run(store, Request(ObjectOperation::kPut, "resp_2")).code ==
            "object_quota_exceeded");
  Q4T_CHECK(
      Ok(Run(store, Request(ObjectOperation::kPut, "resp_2", "client:b"))));
  Q4T_CHECK(
      Run(store, Request(ObjectOperation::kPut, "resp_3", "client:c")).code ==
      "object_quota_exceeded");
  Q4T_CHECK(Ok(Run(store, Request(ObjectOperation::kErase))));
  Q4T_CHECK(
      Ok(Run(store, Request(ObjectOperation::kPut, "resp_3", "client:c"))));
  auto health = Run(store, Request(ObjectOperation::kHealth));
  Q4T_CHECK(Ok(health) && health.value.GetInt("objects", -1) == 0 &&
            health.value.GetInt("bytes", -1) == 0);
  return true;
}

Q4T_TEST(object_store_byte_limit_does_not_replace_old_revision) {
  Fixture f;
  f.options.max_object_bytes = 1024;
  f.options.max_client_bytes = 1200;
  ObjectStore store(f.options);
  Q4T_CHECK(Ok(Run(store, Request())));
  auto large = Request();
  large.expected_revision = 1;
  large.data = Parse("\"" + std::string(2000, 'x') + "\"");
  Q4T_CHECK(Run(store, large).http_status == 413);
  Q4T_CHECK(
      Run(store, Request(ObjectOperation::kGet)).value.GetInt("revision") == 1);
  auto medium = Request(ObjectOperation::kPut, "resp_2");
  medium.data = Parse("\"" + std::string(600, 'x') + "\"");
  Q4T_CHECK(Ok(Run(store, medium)));
  const auto usage = Run(store, Request(ObjectOperation::kHealth));
  Q4T_CHECK(Ok(usage) && usage.value.GetInt("bytes") <= 1200 &&
            usage.value.GetInt("objects") == 2);
  Q4T_CHECK(Run(store, Request(ObjectOperation::kPut, "resp_3")).code ==
            "object_quota_exceeded");
  return true;
}

Q4T_TEST(object_store_expiration_reclaims_persistent_quota) {
  Fixture f;
  f.options.max_client_objects = 1;
  ObjectStore store(f.options);
  auto expiring = Request();
  expiring.ttl_seconds = 1;
  const auto written = Run(store, expiring);
  Q4T_CHECK(Ok(written));
  const auto expires = written.value.GetInt("expires_at");
  while (std::chrono::duration_cast<std::chrono::seconds>(
             std::chrono::system_clock::now().time_since_epoch())
             .count() < expires)
    std::this_thread::sleep_for(5ms);
  Q4T_CHECK(Run(store, Request(ObjectOperation::kGet)).http_status == 404);
  Q4T_CHECK(Ok(Run(store, Request(ObjectOperation::kPut, "resp_2"))));
  return true;
}

Q4T_TEST(object_store_private_paths_and_opaque_ids) {
  Fixture f;
  ObjectStore store(f.options);
  Q4T_CHECK(
      Run(store, Request(ObjectOperation::kPut, "../escape")).http_status ==
      400);
  Q4T_CHECK(!fs::exists(f.options.directory));
  Q4T_CHECK(Ok(Run(store, Request())));
  struct stat info {};
  Q4T_CHECK(!stat(f.options.directory.c_str(), &info) &&
            (info.st_mode & 0777) == 0700);
  Q4T_CHECK(!stat(f.Record().c_str(), &info) && (info.st_mode & 0777) == 0600);
  const auto first = NewObjectId(ObjectKind::kResponse),
             second = NewObjectId(ObjectKind::kResponse);
  Q4T_CHECK(first.starts_with("resp_") && first.size() == 37 &&
            first != second);
  return true;
}

Q4T_TEST(object_store_refuses_symlink_and_shared_directory) {
  Fixture f;
  const auto target = f.directory / "target";
  fs::create_directory(target);
  chmod(target.c_str(), 0700);
  fs::create_directory_symlink(target, f.options.directory);
  {
    ObjectStore store(f.options);
    Q4T_CHECK(Run(store, Request()).http_status == 503);
    Q4T_CHECK(fs::is_empty(target) && GrammarContinues());
  }
  fs::remove(f.options.directory);
  fs::create_directory(f.options.directory);
  chmod(f.options.directory.c_str(), 0755);
  ObjectStore store(f.options);
  Q4T_CHECK(Run(store, Request()).http_status == 503);
  Q4T_CHECK(fs::is_empty(f.options.directory));
  return true;
}

Q4T_TEST(object_store_corruption_is_not_missing_or_success) {
  Fixture f;
  {
    ObjectStore store(f.options);
    Q4T_CHECK(Ok(Run(store, Request())));
    std::ofstream(f.Record()) << "{broken";
    auto bad = Run(store, Request(ObjectOperation::kGet));
    Q4T_CHECK(bad.http_status == 503 && bad.code == "object_io_error");
    Q4T_CHECK(GrammarContinues());
  }
  ObjectStore restarted(f.options);
  Q4T_CHECK(Run(restarted, Request(ObjectOperation::kGet)).http_status == 503);
  return true;
}

Q4T_TEST(object_store_fifo_tampering_is_bounded) {
  Fixture f;
  ObjectStore store(f.options);
  Q4T_CHECK(Ok(Run(store, Request())));
  const auto file = f.Record();
  fs::remove(file);
  Q4T_CHECK(!mkfifo(file.c_str(), 0600));
  const auto start = Clock::now();
  Q4T_CHECK(Run(store, Request(ObjectOperation::kGet)).http_status == 503);
  Q4T_CHECK(Clock::now() - start < 500ms && GrammarContinues());
  return true;
}

Q4T_TEST(object_store_directory_lock_is_not_stolen) {
  Fixture f;
  fs::create_directory(f.options.directory);
  chmod(f.options.directory.c_str(), 0700);
  const auto path = fs::path(f.options.directory) / ".q4t-lock";
  const int lock = open(path.c_str(), O_RDWR | O_CREAT | O_CLOEXEC, 0600);
  Q4T_CHECK(lock >= 0 && !flock(lock, LOCK_EX | LOCK_NB));
  ObjectStore store(f.options);
  const auto reply = Run(store, Request());
  close(lock);
  Q4T_CHECK(reply.code == "object_store_owned" && !reply.commit_unknown);
  Q4T_CHECK(Ok(Run(store, Request())));
  Q4T_CHECK(store.Health().workers_started == 2);
  return true;
}

Q4T_TEST(object_store_io_failure_recovers_without_replaying_write) {
  Fixture f;
  ObjectStore store(f.options);
  Q4T_CHECK(Ok(Run(store, Request())));
  Q4T_CHECK(!chmod(f.options.directory.c_str(), 0500));
  const auto failed = Run(store, Request(ObjectOperation::kPut, "resp_2"));
  Q4T_CHECK(!chmod(f.options.directory.c_str(), 0700));
  Q4T_CHECK(failed.http_status == 503 && !failed.commit_unknown);
  Q4T_CHECK(Run(store, Request(ObjectOperation::kGet, "resp_2")).http_status ==
            404);
  Q4T_CHECK(Ok(Run(store, Request(ObjectOperation::kGet))) &&
            GrammarContinues());
  Q4T_CHECK(store.Health().workers_started == 2);
  return true;
}

Q4T_TEST(object_store_timeout_queue_cancellation_and_recovery_budget) {
  Injection inject("--grammar-fixture-silent");
  Fixture f;
  f.options.io_timeout_ms = 1000;
  f.options.max_queue = 1;
  f.options.restart_limit = 1;
  ObjectStore store(f.options);
  std::atomic<bool> cancel{false};
  auto operation = std::async(std::launch::async, [&] {
    return store.Execute(Request(), Clock::now() + 1s,
                         [&] { return cancel.load(); });
  });
  const auto end = Clock::now() + 500ms;
  while (!store.Health().workers_started && Clock::now() < end)
    std::this_thread::sleep_for(1ms);
  Q4T_CHECK(store.Health().workers_started == 1);
  Q4T_CHECK(Run(store, Request()).code == "object_queue_full");
  cancel.store(true);
  auto cancelled = operation.get();
  Q4T_CHECK(cancelled.http_status == 499);
  while (store.Health().pending && Clock::now() < end)
    std::this_thread::sleep_for(1ms);
  const auto blocked = Run(store, Request());
  Q4T_CHECK(blocked.code == "object_recovery_backoff");
  Q4T_CHECK(store.Health().workers_started == 1 && GrammarContinues());
  return true;
}

Q4T_TEST(object_store_stuck_spawn_retains_one_controller) {
  Injection inject;
  Fixture f;
  f.options.io_timeout_ms = 50;
  DeferObjectSpawnForTesting(true);
  auto store = std::make_unique<ObjectStore>(f.options);
  const auto start = Clock::now();
  Q4T_CHECK(Run(*store, Request()).http_status == 504);
  Q4T_CHECK(Clock::now() - start < 250ms);
  store.reset();
  Q4T_CHECK(Clock::now() - start < 400ms);
  {
    ObjectStore replacement(f.options);
    Q4T_CHECK(replacement.Health().state == "controller_owned");
    Q4T_CHECK(Run(replacement, Request()).http_status == 503);
    Q4T_CHECK(GrammarContinues());
  }
  DeferObjectSpawnForTesting(false);
  // The former controller completes its bounded cleanup; no replacement
  // thread may be started while its ownership is retained.
  std::this_thread::sleep_for(150ms);
  f.options.io_timeout_ms = 2000;
  ObjectStore healthy(f.options);
  Q4T_CHECK(Ok(Run(healthy, Request())));
  return true;
}

Q4T_TEST(object_store_lost_receipt_preserves_commit_uncertainty) {
  Injection inject("--q4t-object-worker-lost-ack");
  Fixture f;
  f.options.io_timeout_ms = 200;
  ObjectStore store(f.options);
  const auto failed = Run(store, Request());
  Q4T_CHECK(!failed.ok() && failed.commit_unknown);
  const auto end = Clock::now() + 500ms;
  while (store.Health().pending && Clock::now() < end)
    std::this_thread::sleep_for(1ms);
  const auto confirmed = Run(store, Request(ObjectOperation::kGet));
  Q4T_CHECK(Ok(confirmed) && confirmed.value.GetInt("revision") == 1);
  Q4T_CHECK(store.Health().workers_started == 2);
  Q4T_CHECK(Run(store, Request()).http_status == 409);
  Q4T_CHECK(GrammarContinues());
  return true;
}

Q4T_TEST(object_store_global_bytes_account_across_clients_after_restart) {
  Fixture f;
  f.options.max_object_bytes = 1024;
  f.options.max_client_bytes = 1024;
  f.options.max_total_bytes = 2048;
  auto medium = Request(ObjectOperation::kPut, "resp_2", "client:b");
  medium.data = Parse("\"" + std::string(600, 'x') + "\"");
  {
    ObjectStore store(f.options);
    Q4T_CHECK(Ok(Run(store, Request())));
    Q4T_CHECK(Ok(Run(store, medium)));
  }
  ObjectStore restarted(f.options);
  medium.scope = "client:c";
  Q4T_CHECK(Run(restarted, medium).code == "object_quota_exceeded");
  Q4T_CHECK(Ok(Run(restarted, Request(ObjectOperation::kErase))));
  Q4T_CHECK(Ok(Run(restarted, medium)));
  return true;
}

Q4T_TEST(object_store_reduced_limit_preserves_read_delete_access) {
  Fixture f;
  auto large = Request();
  large.data = Parse("\"" + std::string(2000, 'x') + "\"");
  {
    ObjectStore store(f.options);
    Q4T_CHECK(Ok(Run(store, large)));
  }
  f.options.max_object_bytes = 1024;
  f.options.max_client_bytes = 1024;
  f.options.max_total_bytes = 1024;
  ObjectStore restarted(f.options);
  Q4T_CHECK(Ok(Run(restarted, Request(ObjectOperation::kGet))));
  Q4T_CHECK(Run(restarted, Request(ObjectOperation::kPut, "resp_2")).code ==
            "object_quota_exceeded");
  Q4T_CHECK(Ok(Run(restarted, Request(ObjectOperation::kErase))));
  Q4T_CHECK(Ok(Run(restarted, Request(ObjectOperation::kPut, "resp_2"))));
  return true;
}
