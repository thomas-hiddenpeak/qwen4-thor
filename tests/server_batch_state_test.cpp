#include "q4t/server/batch_state.h"

#include <fcntl.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/files.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/object_retention.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = fs::absolute(output ? output : ".q4t-work/batch-state-cpu");
    fs::create_directories(root);
    auto directory = (root / "batch-state-XXXXXX").string();
    if (!mkdtemp(directory.data())) throw std::runtime_error("batch fixture");
    options.directory = directory + "/objects";
    options.restart_backoff_ms = 0;
    options.io_timeout_ms = 500;
  }
};
struct Injection {
  explicit Injection(std::string value) { ObjectWorkerEntryForTesting(value); }
  ~Injection() { ObjectWorkerEntryForTesting({}); }
};
BinaryAccess Access(std::string scope = "client:batch") {
  return {std::move(scope), Clock::now() + 10s, {}};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%d %s: %s\n", reply.http_status, reply.code.c_str(),
                 reply.message.c_str());
  return reply.ok();
}
std::string File(ObjectStore& objects, const std::string& bytes,
                 std::string purpose = "batch") {
  FileUpload upload(objects, Access(), "multipart/form-data; boundary=b");
  const auto wire =
      "--b\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\n" +
      purpose +
      "\r\n--b\r\nContent-Disposition: form-data; name=\"file\"; "
      "filename=\"input.jsonl\"\r\n\r\n" +
      bytes + "\r\n--b--\r\n";
  if (!upload.Feed(wire, Access().deadline).ok())
    throw std::runtime_error("fixture file feed");
  auto result = upload.Finish();
  if (!Ok(result)) throw std::runtime_error("fixture file finish");
  return result.value.GetString("id");
}
BatchCreate Options(const std::string& file) {
  BatchCreate result;
  result.input_file_id = file;
  result.endpoint = "/v1/responses";
  result.metadata = Object({{"purpose", String("CPU checkpoint fixture")}});
  return result;
}
std::string Create(ObjectStore& objects, const std::string& file) {
  auto reply = BatchState(objects).Create(Access(), Options(file));
  if (!Ok(reply)) throw std::runtime_error("fixture batch create");
  return reply.value.GetString("id");
}
std::string Input(size_t rows) {
  std::string out;
  for (size_t i = 0; i < rows; ++i)
    out +=
        OutputJson(Object({{"custom_id", String("source-" + std::to_string(i))},
                           {"method", String("POST")},
                           {"url", String("/v1/responses")},
                           {"body", Object({{"model", String("fixture")},
                                            {"input", String("资料")}})}})) +
        "\n";
  return out;
}
std::vector<BatchRowPosition> Positions(const std::string& bytes) {
  std::vector<BatchRowPosition> rows;
  BatchInput input(
      "/v1/responses",
      [](const auto&, auto* result) {
        result->canonical_model = "fixture";
        return ObjectReply{};
      },
      [&](auto&& row) {
        rows.push_back(
            {row.line, row.offset, row.wire_bytes, Digest(row.custom_id)});
        return ObjectReply{};
      });
  if (!input.Feed(bytes).ok() || !input.Finish().ok())
    throw std::runtime_error("fixture JSONL validation");
  return rows;
}
ObjectRequest StoredRequest(const std::string& id,
                            ObjectKind kind = ObjectKind::kBatch) {
  ObjectRequest request;
  request.scope = Access().scope;
  request.id = id;
  request.kind = kind;
  return request;
}
ObjectReply Edit(ObjectStore& objects, const std::string& id,
                 const std::function<void(Json&)>& change) {
  auto request = StoredRequest(id);
  auto loaded = objects.Execute(request, Access().deadline);
  if (!loaded.ok()) return loaded;
  request.operation = ObjectOperation::kPut;
  request.expected_revision = loaded.value.GetInt("revision");
  request.created_at = loaded.value.GetInt("created_at");
  request.expires_at = loaded.value.GetInt("expires_at");
  request.data = *loaded.value.Find("data");
  change(request.data);
  return objects.Execute(request, Access().deadline);
}
ObjectReply Collect(ObjectStore& objects) {
  auto plan = objects.Retention(0, 128, "", Access().deadline);
  if (!plan.ok()) return plan;
  return objects.Retention(0, 128, plan.value.GetString("plan_sha256"),
                           Access().deadline);
}
void Drain(ObjectStore& objects) {
  const auto until = Clock::now() + 2s;
  while (objects.Health().pending && Clock::now() < until)
    std::this_thread::sleep_for(1ms);
}
}  // namespace

Q4T_TEST(batch_state_actual_input_pin_survives_file_delete_and_store_restart) {
  Fixture fixture;
  std::string id;
  const std::string bytes(2 * kBinaryChunkBytes + 17, 's');
  {
    ObjectStore objects(fixture.options);
    const auto file = File(objects, bytes);
    id = Create(objects, file);
    Q4T_CHECK(Ok(Files(objects).Erase(Access(), file)) && Ok(Collect(objects)));
  }
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  auto metadata = batches.Retrieve(Access(), id);
  Q4T_CHECK(Ok(metadata) &&
            metadata.value.GetString("status") == "validating" &&
            !metadata.value.Has("blob") && !metadata.value.Has("scope") &&
            !metadata.value.Has("owner") && !metadata.value.Has("pages"));
  BinaryRead read;
  BinaryStore binary(objects);
  Q4T_CHECK(Ok(binary.Open(Access(), ObjectKind::kBatch, id, 60, &read)));
  std::string recovered;
  for (size_t i = 0; i < read.manifest.chunks.size(); ++i) {
    std::string chunk;
    Q4T_CHECK(Ok(binary.Read(Access(), read, i, &chunk)));
    recovered += chunk;
  }
  Q4T_CHECK(recovered == bytes && Ok(binary.Close(Access(), &read)));
  return true;
}

Q4T_TEST(batch_state_create_requires_owned_nonempty_batch_file) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto empty = File(objects, "");
  const auto wrong = File(objects, Input(1), "user_data");
  const auto owned = File(objects, Input(1));
  Q4T_CHECK(batches.Create(Access(), Options(empty)).http_status == 400);
  Q4T_CHECK(batches.Create(Access(), Options(wrong)).http_status == 400);
  Q4T_CHECK(
      batches.Create(Access("client:other"), Options(owned)).http_status ==
      404);
  Q4T_CHECK(
      batches.List(Access(), Object()).value.GetArray("data")->array.empty());
  return true;
}

Q4T_TEST(batch_state_public_list_cursor_and_owner_isolation) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto file = File(objects, Input(1));
  for (size_t i = 0; i < 3; ++i) Create(objects, file);
  auto query = ParseApplicationQuery("limit=2");
  Q4T_CHECK(Ok(query));
  auto first = batches.List(Access(), query.value);
  Q4T_CHECK(Ok(first) && first.value.GetBool("has_more") &&
            first.value.GetArray("data")->array.size() == 2);
  const auto id = first.value.GetString("last_id");
  auto second = batches.List(
      Access(), Object({{"limit", Number(2)}, {"after", String(id)}}));
  Q4T_CHECK(Ok(second) && !second.value.GetBool("has_more") &&
            second.value.GetArray("data")->array.size() == 1);
  Q4T_CHECK(batches.Retrieve(Access("client:other"), id).http_status == 404 &&
            batches.Cancel(Access("client:other"), id).http_status == 404 &&
            batches.List(Access("client:other"), Object())
                .value.GetArray("data")
                ->array.empty());
  Q4T_CHECK(
      batches.List(Access(), Object({{"limit", Boolean(true)}})).http_status ==
          400 &&
      batches.List(Access(), Object({{"owner", String("other")}}))
              .http_status == 400);
  return true;
}

Q4T_TEST(batch_state_pages_commit_all_offsets_before_execution_state) {
  Fixture fixture;
  const auto bytes = Input(kBatchPageRows + 1);
  const auto rows = Positions(bytes);
  std::string id;
  {
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    id = Create(objects, File(objects, bytes));
    Q4T_CHECK(Ok(batches.AppendValidationPage(
        Access(), id, std::span(rows).first(kBatchPageRows))));
    Q4T_CHECK(batches.CompleteValidation(Access(), id, "fixture").http_status ==
              400);
    Q4T_CHECK(Ok(batches.AppendValidationPage(
        Access(), id, std::span(rows).subspan(kBatchPageRows))));
    Q4T_CHECK(Ok(batches.CompleteValidation(Access(), id, "fixture")));
  }
  ObjectStore objects(fixture.options);
  auto loaded = BatchState(objects).Load(Access(), id);
  Q4T_CHECK(Ok(loaded));
  const auto& data = *loaded.value.Find("data");
  Q4T_CHECK(data.GetInt("validated_rows") == 129 &&
            data.GetInt("validated_bytes") ==
                static_cast<int64_t>(bytes.size()) &&
            data.GetArray("pages")->array.size() == 2 &&
            data.Find("batch")->GetString("status") == "in_progress" &&
            data.Find("batch")->Find("request_counts")->GetInt("total") == 129);
  for (const auto& page : data.GetArray("pages")->array) {
    const auto stored = objects.Execute(
        StoredRequest(page.str, ObjectKind::kToolRun), Access().deadline);
    Q4T_CHECK(Ok(stored));
    for (const auto& row : stored.value.Find("data")->GetArray("rows")->array)
      Q4T_CHECK(row.GetString("status") == "pending" &&
                row.Find("result_id")->IsNull());
  }
  return true;
}

Q4T_TEST(batch_state_invalid_validation_page_is_atomic) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto bytes = Input(2);
  const auto original = Positions(bytes);
  const auto id = Create(objects, File(objects, bytes));
  const auto before = batches.Load(Access(), id);
  for (const int mode : {0, 1, 2, 3}) {
    auto rows = original;
    if (mode == 0) ++rows[1].offset;
    if (mode == 1) ++rows[1].line;
    if (mode == 2) rows[1].wire_bytes = bytes.size();
    if (mode == 3) rows[1].custom_id_sha256 = "bad";
    Q4T_CHECK(batches.AppendValidationPage(Access(), id, rows).http_status ==
              400);
    Q4T_CHECK(OutputJson(batches.Load(Access(), id).value) ==
              OutputJson(before.value));
  }
  Q4T_CHECK(Ok(batches.AppendValidationPage(Access(), id, original)));
  Q4T_CHECK(batches.AppendValidationPage(Access(), id, original).http_status ==
            400);
  return true;
}

Q4T_TEST(batch_state_cancel_prevents_late_validation_and_waits_for_owner) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto bytes = Input(1);
  const auto id = Create(objects, File(objects, bytes));
  auto cancelled = batches.Cancel(Access(), id);
  Q4T_CHECK(Ok(cancelled) &&
            cancelled.value.GetString("status") == "cancelling");
  const auto before = batches.Load(Access(), id).value.GetInt("revision");
  Q4T_CHECK(Ok(batches.Cancel(Access(), id)) &&
            batches.Load(Access(), id).value.GetInt("revision") == before);
  Q4T_CHECK(batches.AppendValidationPage(Access(), id, Positions(bytes))
                    .http_status == 409 &&
            batches.CompleteValidation(Access(), id, "fixture").http_status ==
                409);
  Q4T_CHECK(Ok(
      Edit(objects, id, [](Json& data) { Set(&data, "running", Number(1)); })));
  Q4T_CHECK(batches.FinishCancelled(Access(), id).http_status == 409);
  Q4T_CHECK(Ok(
      Edit(objects, id, [](Json& data) { Set(&data, "running", Number(0)); })));
  auto finished = batches.FinishCancelled(Access(), id);
  Q4T_CHECK(Ok(finished) && finished.value.GetString("status") == "cancelled");
  Q4T_CHECK(Ok(batches.Cancel(Access(), id)));
  return true;
}

Q4T_TEST(batch_state_failed_validation_keeps_original_line_error) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto id = Create(objects, File(objects, "invalid\n"));
  auto failure =
      Error(400, "duplicate_custom_id", "Original validation failure");
  failure.value = Object({{"line", Number(2)}, {"param", String("custom_id")}});
  auto result = batches.FailValidation(Access(), id, failure);
  Q4T_CHECK(Ok(result) && result.value.GetString("status") == "failed");
  const auto& error = result.value.Find("errors")->GetArray("data")->array[0];
  Q4T_CHECK(error.GetInt("line") == 2 &&
            error.GetString("param") == "custom_id" &&
            error.GetString("code") == "duplicate_custom_id");
  Q4T_CHECK(batches.Cancel(Access(), id).http_status == 409 &&
            batches.CompleteValidation(Access(), id, "fixture").http_status ==
                409);
  return true;
}

Q4T_TEST(batch_state_reset_retires_private_pages_but_preserves_pinned_input) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto bytes = Input(1);
  const auto file = File(objects, bytes);
  const auto id = Create(objects, file);
  auto appended = batches.AppendValidationPage(Access(), id, Positions(bytes));
  Q4T_CHECK(Ok(appended));
  const auto page = appended.value.GetString("page_id");
  Q4T_CHECK(Ok(batches.ResetValidation(Access(), id)) &&
            Ok(Files(objects).Erase(Access(), file)) && Ok(Collect(objects)));
  Q4T_CHECK(
      objects
          .Execute(StoredRequest(page, ObjectKind::kToolRun), Access().deadline)
          .http_status == 404);
  BinaryRead read;
  BinaryStore binary(objects);
  Q4T_CHECK(Ok(binary.Open(Access(), ObjectKind::kBatch, id, 60, &read)));
  std::string restored;
  Q4T_CHECK(Ok(binary.Read(Access(), read, 0, &restored)) &&
            restored == bytes && Ok(binary.Close(Access(), &read)));
  return true;
}

Q4T_TEST(
    batch_state_actual_unknown_create_receipt_is_retrievable_without_replay) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  std::string file;
  {
    ObjectStore objects(fixture.options);
    file = File(objects, Input(1));
  }
  Injection injection("--q4t-object-worker-application-lost-ack");
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  auto result = batches.Create(Access(), Options(file));
  Q4T_CHECK(!result.ok() && result.commit_unknown &&
            result.value.GetString("id").starts_with("batch_"));
  Drain(objects);
  auto restored = batches.Retrieve(Access(), result.value.GetString("id"));
  Q4T_CHECK(
      Ok(restored) && restored.value.GetString("status") == "validating" &&
      batches.List(Access(), Object()).value.GetArray("data")->array.size() ==
          1);
  return true;
}

Q4T_TEST(batch_state_actual_unknown_page_receipt_commits_root_and_page_once) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  const auto bytes = Input(1);
  std::string id;
  {
    ObjectStore objects(fixture.options);
    id = Create(objects, File(objects, bytes));
  }
  Injection injection("--q4t-object-worker-application-lost-ack");
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  auto result = batches.AppendValidationPage(Access(), id, Positions(bytes));
  Q4T_CHECK(!result.ok() && result.commit_unknown &&
            result.value.GetString("id") == id);
  Drain(objects);
  auto restored = batches.Load(Access(), id);
  Q4T_CHECK(Ok(restored));
  const auto& data = *restored.value.Find("data");
  Q4T_CHECK(data.GetInt("validated_rows") == 1 &&
            data.GetArray("pages")->array.size() == 1 &&
            data.GetArray("pages")->array[0].str ==
                result.value.GetString("page_id"));
  auto page = objects.Execute(
      StoredRequest(result.value.GetString("page_id"), ObjectKind::kToolRun),
      Access().deadline);
  Q4T_CHECK(Ok(page) && page.value.GetInt("revision") == 1);
  return true;
}

Q4T_TEST(batch_state_auxiliary_pin_cleanup_failure_keeps_creation_success) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 500;
  std::string file;
  {
    ObjectStore objects(fixture.options);
    file = File(objects, Input(1));
  }
  Injection injection("--q4t-object-worker-read-close-lost-ack");
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  auto result = batches.Create(Access(), Options(file));
  Q4T_CHECK(Ok(result) && result.value.GetString("status") == "validating" &&
            objects.Health().auxiliary_cleanup_failures == 1);
  Drain(objects);
  Q4T_CHECK(Ok(batches.Retrieve(Access(), result.value.GetString("id"))));
  return true;
}

Q4T_TEST(batch_state_live_and_unknown_owner_never_grant_recovery) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto id = Create(objects, File(objects, Input(1)));
  Q4T_CHECK(batches.ClaimAbandoned(Access(), id).code ==
            "batch_owner_unconfirmed");
  Q4T_CHECK(
      Ok(Edit(objects, id, [](Json& data) { Set(&data, "owner", Object()); })));
  const auto before = batches.Load(Access(), id);
  Q4T_CHECK(
      batches.ClaimAbandoned(Access(), id).code == "batch_owner_unconfirmed" &&
      OutputJson(batches.Load(Access(), id).value) == OutputJson(before.value));
  return true;
}

Q4T_TEST(batch_state_actual_dead_process_claim_preserves_original_deadline) {
  Fixture fixture;
  int fds[2];
  Q4T_CHECK(pipe2(fds, O_CLOEXEC) == 0);
  const pid_t pid = fork();
  Q4T_CHECK(pid >= 0);
  if (pid == 0) {
    close(fds[0]);
    try {
      std::string id;
      {
        ObjectStore objects(fixture.options);
        id = Create(objects, File(objects, Input(1)));
      }
      if (write(fds[1], id.data(), id.size()) !=
          static_cast<ssize_t>(id.size()))
        _exit(3);
      close(fds[1]);
      _exit(0);
    } catch (...) {
      _exit(2);
    }
  }
  close(fds[1]);
  char bytes[128];
  const auto length = read(fds[0], bytes, sizeof(bytes));
  close(fds[0]);
  int status = 0;
  Q4T_CHECK(waitpid(pid, &status, 0) == pid && WIFEXITED(status) &&
            WEXITSTATUS(status) == 0 && length > 0);
  const std::string id(bytes, length);
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto before = batches.Retrieve(Access(), id);
  Q4T_CHECK(Ok(before));
  auto claimed = batches.ClaimAbandoned(Access(), id);
  Q4T_CHECK(
      Ok(claimed) &&
      claimed.value.GetInt("created_at") == before.value.GetInt("created_at") &&
      claimed.value.GetInt("expires_at") == before.value.GetInt("expires_at"));
  const auto loaded = batches.Load(Access(), id);
  Q4T_CHECK(
      Ok(loaded) && loaded.value.Find("data")->GetInt("recoveries") == 1 &&
      loaded.value.Find("data")->Find("owner")->GetInt("pid") == getpid());
  return true;
}

Q4T_TEST(batch_state_original_expiry_blocks_late_validation_mutation) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto bytes = Input(1);
  const auto source = Create(objects, File(objects, bytes));
  auto loaded = batches.Load(Access(), source);
  Q4T_CHECK(Ok(loaded));
  const auto id = NewObjectId(ObjectKind::kBatch);
  auto request = StoredRequest(id);
  request.operation = ObjectOperation::kPut;
  request.created_at = std::time(nullptr) - kBatchWindowSeconds - 1;
  request.expires_at = loaded.value.GetInt("expires_at");
  request.data = *loaded.value.Find("data");
  Set(&Field(request.data, "batch"), "id", String(id));
  Set(&Field(request.data, "batch"), "created_at", Number(request.created_at));
  Set(&Field(request.data, "batch"), "expires_at",
      Number(request.created_at + kBatchWindowSeconds));
  Q4T_CHECK(Ok(objects.Execute(request, Access().deadline)));
  const auto before = batches.Load(Access(), id);
  Q4T_CHECK(Ok(before));
  Q4T_CHECK(!batches.AppendValidationPage(Access(), id, Positions(bytes)).ok());
  Q4T_CHECK(!batches.ResetValidation(Access(), id).ok());
  Q4T_CHECK(OutputJson(batches.Load(Access(), id).value) ==
            OutputJson(before.value));
  // Expiry never prevents explicitly cancelling an abandoned validation.
  Q4T_CHECK(Ok(batches.Cancel(Access(), id)) &&
            Ok(batches.FinishCancelled(Access(), id)));
  return true;
}

Q4T_TEST(batch_state_corrupt_durable_counters_never_authorize_execution) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto file = File(objects, Input(1));
  for (const int mode : {0, 1, 2, 3, 4, 5}) {
    const auto id = Create(objects, file);
    Q4T_CHECK(Ok(Edit(objects, id, [&](Json& data) {
      if (mode == 0) Set(&data, "running", Boolean(false));
      if (mode == 1) Set(&data, "next_row", Number(1));
      if (mode == 2) Set(&data, "validated_rows", Number(1));
      if (mode == 3) Set(&data, "output_ttl_seconds", Number(1));
      if (mode == 4)
        Set(&Field(Field(data, "batch"), "request_counts"), "completed",
            Number(1));
      if (mode == 5) Set(&Field(data, "batch"), "expires_at", String("123"));
    })));
    Q4T_CHECK(batches.Retrieve(Access(), id).code == "batch_state_invalid" &&
              batches.CompleteValidation(Access(), id, "fixture").code ==
                  "batch_state_invalid");
  }
  return true;
}

Q4T_TEST(
    batch_state_retention_tracks_private_pages_without_extending_output_ttl) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto bytes = Input(1);
  const auto id = Create(objects, File(objects, bytes));
  auto appended = batches.AppendValidationPage(Access(), id, Positions(bytes));
  Q4T_CHECK(Ok(appended));
  auto loaded = batches.Load(Access(), id);
  Q4T_CHECK(Ok(loaded));
  auto data = *loaded.value.Find("data");
  const auto page_id = appended.value.GetString("page_id");
  Set(&Field(data, "batch"), "output_file_id", String("file_output"));
  auto retention = InspectObjectRetention("batch", data);
  Q4T_CHECK(retention.active && !retention.uncertain &&
            std::find(retention.references.begin(), retention.references.end(),
                      page_id) != retention.references.end() &&
            std::find(retention.references.begin(), retention.references.end(),
                      "file_output") == retention.references.end());
  auto page = objects.Execute(StoredRequest(page_id, ObjectKind::kToolRun),
                              Access().deadline);
  Q4T_CHECK(Ok(page));
  auto page_data = *page.value.Find("data");
  Set(&Field(page_data, "rows").array[0], "result_id",
      String("tool_run_result"));
  retention = InspectObjectRetention("tool_run", page_data);
  Q4T_CHECK(!retention.uncertain && retention.collect_when_unreferenced &&
            retention.references ==
                std::vector<std::string>{"tool_run_result"});
  Set(&Field(page_data, "rows").array[0], "status", String("unknown"));
  Q4T_CHECK(InspectObjectRetention("tool_run", page_data).uncertain);
  return true;
}
