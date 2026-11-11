#include "q4t/server/batch_state.h"
#include "q4t/server/batch_checkpoint.h"
#include "q4t/server/batch_preparation.h"
#include "q4t/server/batch_usage.h"

#include <fcntl.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <barrier>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <future>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/files.h"
#include "q4t/server/object_retention.h"
#include "q4t/server/responses_owner.h"
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
    auto root = fs::absolute(output ? output : ".q4t-work/batch-results-cpu");
    fs::create_directories(root);
    auto directory = (root / "batch-results-XXXXXX").string();
    if (!mkdtemp(directory.data()))
      throw std::runtime_error("fixture directory");
    options.directory = directory + "/objects";
    options.restart_backoff_ms = 0;
    options.io_timeout_ms = 500;
  }
};
struct Injection {
  explicit Injection(std::string value) { ObjectWorkerEntryForTesting(value); }
  ~Injection() { ObjectWorkerEntryForTesting({}); }
};
BinaryAccess Access(std::string scope = "client:batch-results") {
  return {std::move(scope), Clock::now() + 30s, {}};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%d %s: %s\n", reply.http_status, reply.code.c_str(),
                 reply.message.c_str());
  return reply.ok();
}
Json Parsed(const std::string& text) {
  Json value;
  if (!Parse(text, &value)) throw std::runtime_error("fixture JSON");
  return value;
}
ObjectRequest Request(const std::string& id,
                      ObjectKind kind = ObjectKind::kBatch) {
  ObjectRequest request;
  request.scope = Access().scope;
  request.id = id;
  request.kind = kind;
  return request;
}
ObjectReply Edit(ObjectStore& objects, const std::string& id, ObjectKind kind,
                 const std::function<void(ObjectRequest&)>& change) {
  auto request = Request(id, kind);
  auto loaded = objects.Execute(request, Access().deadline);
  if (!loaded.ok()) return loaded;
  request.operation = ObjectOperation::kPut;
  request.expected_revision = loaded.value.GetInt("revision");
  request.created_at = loaded.value.GetInt("created_at");
  request.expires_at = loaded.value.GetInt("expires_at");
  request.data = *loaded.value.Find("data");
  change(request);
  return objects.Execute(request, Access().deadline);
}
std::string Input(size_t count, size_t padding = 0) {
  std::string text;
  for (size_t i = 0; i < count; ++i) {
    text += OutputJson(Object(
        {{"custom_id", String("请求\n\"" + std::to_string(i))},
         {"method", String("POST")},
         {"url", String("/v1/responses")},
         {"body", Object({{"model", String("CPU-fixture")},
                          {"input", String(i == 0 ? std::string(padding, 'x')
                                                  : "资料")}})}}));
    if (i + 1 != count) text += "\r\n";
  }
  return text;
}
struct ReadyBatch {
  std::string id, file;
  std::vector<BatchInputRow> rows;
};
ReadyBatch Ready(ObjectStore& objects, size_t count, size_t padding = 0,
                 bool freeze = false) {
  const auto bytes = Input(count, padding);
  FileUpload upload(objects, Access(), "multipart/form-data; boundary=b");
  const auto wire =
      "--b\r\nContent-Disposition: form-data; "
      "name=\"purpose\"\r\n\r\nbatch\r\n"
      "--b\r\nContent-Disposition: form-data; name=\"file\"; "
      "filename=\"input.jsonl\"\r\n\r\n" +
      bytes + "\r\n--b--\r\n";
  if (!Ok(upload.Feed(wire, Access().deadline)))
    throw std::runtime_error("fixture upload feed");
  auto uploaded = upload.Finish();
  if (!Ok(uploaded)) throw std::runtime_error("fixture upload finish");
  ReadyBatch ready;
  ready.file = uploaded.value.GetString("id");
  BatchCreate options;
  options.input_file_id = ready.file;
  options.endpoint = "/v1/responses";
  options.output_ttl_seconds = 3600;
  BatchState batches(objects);
  auto created = batches.Create(Access(), options);
  if (!Ok(created)) throw std::runtime_error("fixture batch create");
  ready.id = created.value.GetString("id");
  BatchInput input(
      options.endpoint,
      [](const auto&, auto* check) {
        check->canonical_model = "CPU-fixture";
        return ObjectReply{};
      },
      [&](auto&& row) {
        ready.rows.push_back(std::move(row));
        return ObjectReply{};
      });
  if (!Ok(input.Feed(bytes)) || !Ok(input.Finish()))
    throw std::runtime_error("fixture validation");
  std::vector<BatchRowPosition> positions;
  ChatPlanConfig config;
  config.model = "CPU-fixture";
  for (const auto& row : ready.rows) {
    BatchRowPosition position{row.line, row.offset, row.wire_bytes,
                              Digest(row.custom_id)};
    if (freeze) {
      BatchPreparedInput prepared;
      if (!Ok(BatchPreparation(objects).Prepare(
              Access(), ready.id, options.endpoint, row, config, &prepared)) ||
          !prepared.reference.id.empty() || !prepared.hold.id.empty() ||
          !ValidBatchInlineInput(prepared.inline_input))
        throw std::runtime_error("inline preparation fixture");
      position.prepared_binding_sha256 = prepared.binding_sha256;
      position.prepared_inline = std::move(prepared.inline_input);
    }
    positions.push_back(std::move(position));
  }
  for (size_t i = 0; i < count; i += kBatchPageRows)
    if (!Ok(batches.AppendValidationPage(
            Access(), ready.id,
            std::span(positions).subspan(i,
                                         std::min(kBatchPageRows, count - i)))))
      throw std::runtime_error("fixture page");
  if (!Ok(batches.CompleteValidation(Access(), ready.id, "CPU-fixture")))
    throw std::runtime_error("fixture ready");
  return ready;
}
BatchRowResult Start(BatchState& batches, const ReadyBatch& ready, size_t index,
                     int code = 200, std::string body = "{\"answer\":42}") {
  auto started = batches.StartRow(Access(), ready.id, index);
  if (!Ok(started)) throw std::runtime_error("fixture start");
  return {started.value.GetString("request_id"), code, std::move(body), {}};
}
std::string Content(ObjectStore& objects, const std::string& id) {
  BinaryRead read;
  if (!Ok(Files(objects).Open(Access(), id, &read)))
    throw std::runtime_error("fixture file read");
  BinaryStore binary(objects);
  std::string bytes;
  for (size_t i = 0; i < read.manifest.chunks.size(); ++i) {
    std::string chunk;
    if (!Ok(binary.Read(Access(), read, i, &chunk)))
      throw std::runtime_error("fixture chunk");
    bytes += chunk;
  }
  if (!Ok(binary.Close(Access(), &read)))
    throw std::runtime_error("fixture read close");
  return bytes;
}
std::vector<Json> Lines(const std::string& text) {
  std::vector<Json> lines;
  size_t offset = 0;
  while (offset < text.size()) {
    const auto end = text.find('\n', offset);
    if (end == std::string::npos)
      throw std::runtime_error("missing terminal newline");
    lines.push_back(Parsed(text.substr(offset, end - offset)));
    offset = end + 1;
  }
  return lines;
}
ObjectReply Collect(ObjectStore& objects) {
  auto plan = objects.Retention(0, 128, "", Access().deadline);
  return plan.ok()
             ? objects.Retention(0, 128, plan.value.GetString("plan_sha256"),
                                 Access().deadline)
             : plan;
}
void Drain(ObjectStore& objects) {
  const auto until = Clock::now() + 2s;
  while (objects.Health().pending && Clock::now() < until)
    std::this_thread::sleep_for(1ms);
}
}  // namespace

namespace {
std::string UsageBody(uint64_t input = 10, uint64_t output = 4,
                      uint64_t cached = 3, uint64_t reasoning = 2,
                      size_t padding = 0) {
  return OutputJson(
      Object({{"usage", BatchUsageJson({input, output, cached, reasoning})},
              {"text", String(std::string(padding, 'x'))}}));
}
ObjectReply LegacyUsage(ObjectStore& objects, const std::string& id) {
  return Edit(objects, id, ObjectKind::kBatch, [](auto& request) {
    std::erase_if(request.data.object, [](const auto& pair) {
      return pair.first == "usage_accounting";
    });
    std::erase_if(Field(request.data, "batch").object,
                  [](const auto& pair) { return pair.first == "usage"; });
  });
}
}  // namespace

Q4T_TEST(batch_usage_normalizes_endpoints_without_inventing_missing_details) {
  auto body = Parsed(UsageBody());
  const auto response = BatchResponseUsage("/v1/responses", body);
  Q4T_CHECK(response && response->input == 10 && response->output == 4 &&
            response->cached == 3 && response->reasoning == 2);
  const auto chat = Parsed(
      R"({"usage":{"prompt_tokens":9,"completion_tokens":5,"total_tokens":14,"prompt_tokens_details":{"cached_tokens":2},"completion_tokens_details":{"reasoning_tokens":3}}})");
  const auto counted = BatchResponseUsage("/v1/chat/completions", chat);
  Q4T_CHECK(counted && counted->input == 9 && counted->reasoning == 3);
  const auto opaque = Parsed(
      R"({"usage":{"prompt_tokens":9,"completion_tokens":5,"total_tokens":14}})");
  const auto completion = BatchResponseUsage("/v1/completions", opaque);
  Q4T_CHECK(completion && completion->cached == 0 &&
            completion->reasoning == 0);
  Q4T_CHECK(!BatchResponseUsage("/v1/chat/completions", opaque));
  for (
      const auto* invalid :
      {R"({})", R"({"usage":null})",
       R"({"usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3}})",
       R"({"usage":{"input_tokens":-1,"output_tokens":2,"total_tokens":1,"input_tokens_details":{"cached_tokens":0},"output_tokens_details":{"reasoning_tokens":0}}})",
       R"({"usage":{"input_tokens":1,"output_tokens":2,"total_tokens":4,"input_tokens_details":{"cached_tokens":0},"output_tokens_details":{"reasoning_tokens":0}}})",
       R"({"usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3,"input_tokens_details":{"cached_tokens":2},"output_tokens_details":{"reasoning_tokens":0}}})",
       R"({"usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3,"input_tokens_details":{"cached_tokens":0},"output_tokens_details":{"reasoning_tokens":3}}})"})
    Q4T_CHECK(!BatchResponseUsage("/v1/responses", Parsed(invalid)));
  Q4T_CHECK(!BatchResponseUsage("/v1/embeddings", body));
  for (const auto* field : {"input_tokens", "output_tokens", "total_tokens"}) {
    auto huge = Parsed(UsageBody(0, 0, 0, 0));
    Set(&Field(huge, "usage"), field, *Parsed("{\"n\":1e20}").Find("n"));
    Q4T_CHECK(!BatchResponseUsage("/v1/responses", huge));
  }
  return true;
}

Q4T_TEST(batch_usage_atomic_success_error_and_final_files_keep_actual_totals) {
  Fixture fixture;
  ReadyBatch ready;
  {
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    ready = Ready(objects, 2);
    const auto first = Start(batches, ready, 0, 200, UsageBody());
    Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, 0,
                                   ready.rows[0].custom_id, first)));
    const auto second = Start(batches, ready, 1, 500, UsageBody(5, 2, 1, 1));
    Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, 1,
                                   ready.rows[1].custom_id, second)));
    Q4T_CHECK(
        batches
            .FinishRow(Access(), ready.id, 1, ready.rows[1].custom_id, second)
            .http_status == 409);
  }
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  Q4T_CHECK(Ok(batches.BeginFinalization(Access(), ready.id, "completed")));
  const auto final = batches.Finalize(Access(), ready.id);
  Q4T_CHECK(Ok(final) && final.value.Find("usage") &&
            OutputJson(*final.value.Find("usage")) ==
                OutputJson(BatchUsageJson({15, 6, 4, 3})));
  const auto output =
      Lines(Content(objects, final.value.GetString("output_file_id")));
  const auto error =
      Lines(Content(objects, final.value.GetString("error_file_id")));
  Q4T_CHECK(output.size() == 1 && error.size() == 1 &&
            error[0].Find("response")->GetInt("status_code") == 500 &&
            error[0].Find("response")
                    ->Find("body")
                    ->Find("usage")
                    ->GetInt("input_tokens") == 5);
  return true;
}

Q4T_TEST(batch_usage_unknown_running_result_never_becomes_zero) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto ready = Ready(objects, 2);
  auto result = Start(batches, ready, 0);
  result.response_body.clear();
  result.error = Error(503, "batch_execution_interrupted", "Unknown result");
  Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, 0, ready.rows[0].custom_id,
                                 result)));
  Q4T_CHECK(Ok(batches.Cancel(Access(), ready.id)) &&
            Ok(batches.BeginFinalization(Access(), ready.id, "cancelled")));
  BatchRowResult skipped;
  skipped.error = Error(499, "batch_cancelled", "Not started");
  Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, 1, ready.rows[1].custom_id,
                                 skipped)));
  const auto final = batches.Finalize(Access(), ready.id);
  const auto root = batches.Load(Access(), ready.id);
  Q4T_CHECK(Ok(final) && !final.value.Has("usage") && Ok(root));
  const auto* accounting = root.value.Find("data")->Find("usage_accounting");
  Q4T_CHECK(accounting && accounting->GetInt("rows") == 2 &&
            accounting->GetInt("unknown_rows") == 1);
  const auto unstarted = Ready(objects, 1);
  Q4T_CHECK(
      Ok(batches.Cancel(Access(), unstarted.id)) &&
      Ok(batches.BeginFinalization(Access(), unstarted.id, "cancelled")) &&
      Ok(batches.FinishRow(Access(), unstarted.id, 0,
                           unstarted.rows[0].custom_id, skipped)));
  const auto zero = batches.Finalize(Access(), unstarted.id);
  Q4T_CHECK(Ok(zero) && zero.value.Find("usage") &&
            zero.value.Find("usage")->GetInt("total_tokens") == 0);
  return true;
}

Q4T_TEST(
    batch_usage_exact_integer_overflow_and_corruption_never_publish_false_total) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto ready = Ready(objects, 2);
  for (size_t i = 0; i < 2; ++i) {
    const auto result = Start(batches, ready, i, 200,
                              UsageBody(i ? 1 : kBatchTokenLimit, 0, 0, 0));
    Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, i,
                                   ready.rows[i].custom_id, result)));
  }
  const auto root = batches.Load(Access(), ready.id);
  Q4T_CHECK(Ok(root) && !root.value.Find("data")->Find("batch")->Has("usage"));
  const auto* accounting = root.value.Find("data")->Find("usage_accounting");
  Q4T_CHECK(accounting->GetInt("unknown_rows") == 1 &&
            accounting->Find("totals")->GetInt("input_tokens") ==
                static_cast<int64_t>(kBatchTokenLimit));
  Q4T_CHECK(Ok(Edit(objects, ready.id, ObjectKind::kBatch, [](auto& request) {
    auto& accounting = Field(request.data, "usage_accounting");
    Set(&accounting, "rows", *Parsed("{\"n\":1e20}").Find("n"));
    Set(&accounting, "unknown_rows", Number(0));
    Set(&accounting, "totals", BatchUsageJson({}));
  })));
  Q4T_CHECK(batches.Retrieve(Access(), ready.id).code == "batch_state_invalid");
  return true;
}

Q4T_TEST(
    batch_usage_legacy_inline_binary_recount_resume_without_result_rewrite) {
  Fixture fixture;
  ReadyBatch ready;
  std::array<std::string, 2> original;
  {
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    ready = Ready(objects, 2);
    for (size_t i = 0; i < 2; ++i) {
      const auto result = Start(batches, ready, i, 200,
                                UsageBody(10, 4, 3, 2, i ? (2 << 20) : 0));
      Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, i,
                                     ready.rows[i].custom_id, result)));
      original[i] = OutputJson(batches.Row(Access(), ready.id, i).value);
    }
    Q4T_CHECK(Ok(LegacyUsage(objects, ready.id)) &&
              !batches.Retrieve(Access(), ready.id).value.Has("usage"));
    Q4T_CHECK(batches.BeginFinalization(Access(), ready.id, "completed").code ==
              "batch_usage_recount_required");
    Q4T_CHECK(Ok(batches.RecountUsage(Access(), ready.id)));
    const auto root = batches.Load(Access(), ready.id);
    Q4T_CHECK(Ok(root) && BatchUsageRows(*root.value.Find("data")) == 1 &&
              !root.value.Find("data")->Find("batch")->Has("usage"));
  }
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  Q4T_CHECK(Ok(batches.RecountUsage(Access(), ready.id)));
  const auto before = batches.Load(Access(), ready.id);
  Q4T_CHECK(Ok(before) && Ok(batches.RecountUsage(Access(), ready.id)));
  const auto after = batches.Load(Access(), ready.id);
  Q4T_CHECK(Ok(after) && OutputJson(before.value) == OutputJson(after.value));
  for (size_t i = 0; i < 2; ++i)
    Q4T_CHECK(OutputJson(batches.Row(Access(), ready.id, i).value) ==
              original[i]);
  Q4T_CHECK(Ok(batches.BeginFinalization(Access(), ready.id, "completed")));
  const auto final = batches.Finalize(Access(), ready.id);
  Q4T_CHECK(Ok(final) && final.value.Find("usage") &&
            OutputJson(*final.value.Find("usage")) ==
                OutputJson(BatchUsageJson({20, 8, 6, 4})));
  Q4T_CHECK(Content(objects, final.value.GetString("output_file_id"))
                .find(std::string(2 << 20, 'x')) != std::string::npos);
  return true;
}

Q4T_TEST(batch_usage_actual_lost_recount_receipt_never_doubles_prefix) {
  Fixture fixture;
  ReadyBatch ready;
  {
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    ready = Ready(objects, 2);
    for (size_t i = 0; i < 2; ++i) {
      const auto result = Start(batches, ready, i, 200, UsageBody());
      Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, i,
                                     ready.rows[i].custom_id, result)));
    }
    Q4T_CHECK(Ok(LegacyUsage(objects, ready.id)));
  }
  {
    Injection injection("--q4t-object-worker-application-lost-ack");
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    const auto missing = batches.RecountUsage(Access(), ready.id);
    Q4T_CHECK(!missing.ok() && missing.commit_unknown);
    Drain(objects);
  }
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto recovered = batches.Load(Access(), ready.id);
  Q4T_CHECK(Ok(recovered) &&
            BatchUsageRows(*recovered.value.Find("data")) == 1);
  Q4T_CHECK(Ok(batches.RecountUsage(Access(), ready.id)));
  const auto final = batches.Retrieve(Access(), ready.id);
  Q4T_CHECK(Ok(final) && final.value.Find("usage") &&
            final.value.Find("usage")->GetInt("total_tokens") == 28);
  return true;
}

Q4T_TEST(
    batch_results_read_original_rows_across_actual_chunks_after_file_delete) {
  Fixture fixture;
  ReadyBatch ready;
  {
    ObjectStore objects(fixture.options);
    ready = Ready(objects, 3, 2 * kBinaryChunkBytes + 17);
    Q4T_CHECK(Ok(Files(objects).Erase(Access(), ready.file)) &&
              Ok(Collect(objects)));
  }
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  for (size_t i = 0; i < ready.rows.size(); ++i) {
    BatchInputRow row;
    Q4T_CHECK(Ok(batches.ReadInputRow(Access(), ready.id, i, &row)) &&
              row.line == ready.rows[i].line &&
              row.offset == ready.rows[i].offset &&
              row.wire_bytes == ready.rows[i].wire_bytes &&
              row.custom_id == ready.rows[i].custom_id &&
              OutputJson(row.body) == OutputJson(ready.rows[i].body));
  }
  BatchInputRow unchanged;
  unchanged.custom_id = "unchanged";
  Q4T_CHECK(
      batches.ReadInputRow(Access("client:other"), ready.id, 0, &unchanged)
              .http_status == 404 &&
      unchanged.custom_id == "unchanged");
  Q4T_CHECK(batches.Row(Access(), ready.id, 3).http_status == 409);
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  Q4T_CHECK(!batches.ReadInputRow(cancelled, ready.id, 0, &unchanged).ok() &&
            unchanged.custom_id == "unchanged");
  return true;
}

Q4T_TEST(
    batch_results_one_durable_start_ticket_and_atomic_full_endpoint_output) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto ready = Ready(objects, 2);
  Q4T_CHECK(
      batches.StartRow(Access(), ready.id, 1).http_status == 409 &&
      batches.BeginFinalization(Access(), ready.id, "completed").http_status ==
          409);
  auto result =
      Start(batches, ready, 0, 200,
            "{\n\"id\":\"resp_fixture\",\"output\":[{\"type\":\"function_"
            "call\",\"name\":\"lookup\",\"arguments\":\"{}\"}],"
            "\"usage\":{\"input_tokens\":17,\"output_tokens\":3,\"total_"
            "tokens\":20},\"decimal\":1e-999\n}");
  Q4T_CHECK(batches.StartRow(Access(), ready.id, 0).http_status == 409 &&
            batches.StartRow(Access(), ready.id, 1).http_status == 409);
  auto wrong = result;
  wrong.request_id += "x";
  Q4T_CHECK(
      batches.FinishRow(Access(), ready.id, 0, ready.rows[0].custom_id, wrong)
              .http_status == 409 &&
      batches.FinishRow(Access(), ready.id, 0, "wrong ID", result).code ==
          "batch_state_invalid");
  Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, 0, ready.rows[0].custom_id,
                                 result)));
  Q4T_CHECK(
      batches.FinishRow(Access(), ready.id, 0, ready.rows[0].custom_id, result)
          .http_status == 409);
  auto row = batches.Row(Access(), ready.id, 0);
  Q4T_CHECK(Ok(row) && row.value.GetString("status") == "completed");
  Q4T_CHECK(row.value.Find("result_id")->IsNull() &&
            ValidBatchInlineResult(row.value));
  auto next = Start(batches, ready, 1);
  Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, 1, ready.rows[1].custom_id,
                                 next)) &&
            Ok(batches.BeginFinalization(Access(), ready.id, "completed")));
  auto completed = batches.Finalize(Access(), ready.id);
  Q4T_CHECK(Ok(completed) &&
            completed.value.GetString("status") == "completed" &&
            completed.value.Find("error_file_id")->IsNull());
  const auto lines =
      Lines(Content(objects, completed.value.GetString("output_file_id")));
  Q4T_CHECK(lines.size() == 2 &&
            lines[0].GetString("custom_id") == ready.rows[0].custom_id &&
            lines[0].Find("response")->GetString("request_id") ==
                result.request_id &&
            OutputJson(*lines[0].Find("response")->Find("body")) ==
                OutputJson(Parsed(result.response_body)) &&
            lines[0].Find("error")->IsNull());
  return true;
}

Q4T_TEST(batch_results_concurrent_start_has_exactly_one_execution_owner) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto ready = Ready(objects, 1);
  std::barrier barrier(2);
  const auto start = [&] {
    barrier.arrive_and_wait();
    return batches.StartRow(Access(), ready.id, 0);
  };
  auto a = std::async(std::launch::async, start),
       b = std::async(std::launch::async, start);
  const auto ra = a.get(), rb = b.get();
  Q4T_CHECK(ra.ok() != rb.ok());
  Q4T_CHECK((ra.ok() ? rb : ra).http_status == 409);
  auto row = batches.Row(Access(), ready.id, 0);
  Q4T_CHECK(Ok(row) && row.value.GetString("request_id") ==
                           (ra.ok() ? ra : rb).value.GetString("request_id"));
  return true;
}

Q4T_TEST(
    batch_results_mixed_files_restart_and_terminal_private_resource_reclamation) {
  Fixture fixture;
  std::string id, output, error, page_id, original_chunk;
  {
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    const auto ready = Ready(objects, 3);
    id = ready.id;
    auto loaded = batches.Load(Access(), id);
    page_id = loaded.value.Find("data")->GetArray("pages")->array[0].str;
    original_chunk = loaded.value.Find("data")
                         ->Find("blob")
                         ->GetArray("chunks")
                         ->array[0]
                         .GetString("id");
    for (size_t i = 0; i < 3; ++i) {
      auto result = Start(batches, ready, i, i == 1 ? 400 : 200,
                          i == 1 ? "{\"error\":{\"code\":\"invalid_request\","
                                   "\"message\":\"CPU fixture\"}}"
                                 : "{\"answer\":42}");
      if (i == 2)
        result.error =
            Error(503, "batch_interrupted",
                  "Previous execution outcome unknown; not replayed");
      Q4T_CHECK(Ok(
          batches.FinishRow(Access(), id, i, ready.rows[i].custom_id, result)));
    }
    Q4T_CHECK(ValidBatchInlineResult(batches.Row(Access(), id, 0).value));
    Q4T_CHECK(Ok(batches.BeginFinalization(Access(), id, "completed")));
    auto finalized = batches.Finalize(Access(), id);
    Q4T_CHECK(Ok(finalized) &&
              finalized.value.Find("request_counts")->GetInt("completed") ==
                  1 &&
              finalized.value.Find("request_counts")->GetInt("failed") == 2);
    output = finalized.value.GetString("output_file_id");
    error = finalized.value.GetString("error_file_id");
    Q4T_CHECK(batches.Row(Access(), id, 0).http_status == 409);
    for (const auto& file_id : {output, error}) {
      auto file = Files(objects).Retrieve(Access(), file_id);
      Q4T_CHECK(Ok(file) && file.value.GetString("purpose") == "batch_output" &&
                file.value.GetInt("expires_at") -
                        file.value.GetInt("created_at") ==
                    3600 &&
                Files(objects)
                        .Retrieve(Access("client:other"), file_id)
                        .http_status == 404);
    }
    Q4T_CHECK(Ok(Files(objects).Erase(Access(), ready.file)) &&
              Ok(Collect(objects)));
    for (const auto& private_id : {page_id, original_chunk})
      Q4T_CHECK(objects
                    .Execute(Request(private_id, ObjectKind::kToolRun),
                             Access().deadline)
                    .http_status == 404);
  }
  ObjectStore objects(fixture.options);
  auto completed = BatchState(objects).Retrieve(Access(), id);
  Q4T_CHECK(Ok(completed) &&
            completed.value.GetString("status") == "completed");
  auto success = Lines(Content(objects, output)),
       failures = Lines(Content(objects, error));
  Q4T_CHECK(success.size() == 1 && failures.size() == 2 &&
            failures[0].Find("response")->GetInt("status_code") == 400 &&
            failures[0].Find("error")->IsNull() &&
            failures[1].Find("response")->IsNull() &&
            failures[1].Find("error")->GetString("code") ==
                "batch_interrupted");
  return true;
}

Q4T_TEST(batch_results_final_files_coalesce_large_rows_and_page_boundaries) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto ready = Ready(objects, kBatchPageRows + 1);
  for (size_t i = 0; i < ready.rows.size(); ++i) {
    const auto body = OutputJson(Object(
        {{"i", Number(i)}, {"payload", String(std::string(16384, 'q'))}}));
    auto result = Start(batches, ready, i, 200, body);
    Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, i,
                                   ready.rows[i].custom_id, result)));
  }
  Q4T_CHECK(Ok(batches.BeginFinalization(Access(), ready.id, "completed")));
  auto final = batches.Finalize(Access(), ready.id);
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "finalizing");
  final = batches.Finalize(Access(), ready.id);
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "completed");
  auto stored = objects.Execute(
      Request(final.value.GetString("output_file_id"), ObjectKind::kFile),
      Access().deadline);
  Q4T_CHECK(Ok(stored));
  BinaryManifest manifest;
  Q4T_CHECK(ParseBinaryManifest(*stored.value.Find("data")->Find("blob"),
                                &manifest) &&
            manifest.bytes > 2 * kBinaryChunkBytes &&
            manifest.chunks.size() == 4 &&
            manifest.chunks[0].bytes == kBinaryChunkBytes &&
            manifest.chunks[1].bytes == kBinaryChunkBytes);
  const auto lines =
      Lines(Content(objects, final.value.GetString("output_file_id")));
  Q4T_CHECK(lines.size() == ready.rows.size());
  for (size_t i = 0; i < lines.size(); ++i)
    Q4T_CHECK(lines[i].GetString("custom_id") == ready.rows[i].custom_id &&
              lines[i].Find("response")->Find("body")->GetInt("i") ==
                  static_cast<int64_t>(i));
  return true;
}

Q4T_TEST(
    batch_results_cancellation_keeps_finished_and_records_unstarted_without_execution) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto ready = Ready(objects, 3);
  auto result = Start(batches, ready, 0);
  Q4T_CHECK(
      Ok(batches.Cancel(Access(), ready.id)) &&
      batches.BeginFinalization(Access(), ready.id, "cancelled").http_status ==
          409 &&
      batches.FinishCancelled(Access(), ready.id).http_status == 409 &&
      batches.StartRow(Access(), ready.id, 1).http_status == 409);
  Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, 0, ready.rows[0].custom_id,
                                 result)) &&
            Ok(batches.BeginFinalization(Access(), ready.id, "cancelled")));
  const auto revision =
      batches.Load(Access(), ready.id).value.GetInt("revision");
  Q4T_CHECK(Ok(batches.BeginFinalization(Access(), ready.id, "cancelled")) &&
            batches.Load(Access(), ready.id).value.GetInt("revision") ==
                revision);
  Q4T_CHECK(batches.Finalize(Access(), ready.id).http_status == 409);
  for (size_t i = 1; i < 3; ++i) {
    BatchRowResult skipped;
    skipped.error = Error(499, "batch_cancelled",
                          "Batch cancelled before this request started");
    Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, i,
                                   ready.rows[i].custom_id, skipped)));
  }
  auto final = batches.Finalize(Access(), ready.id);
  Q4T_CHECK(
      Ok(final) && final.value.GetString("status") == "cancelled" &&
      final.value.Find("request_counts")->GetInt("completed") == 1 &&
      final.value.Find("request_counts")->GetInt("failed") == 2 &&
      Lines(Content(objects, final.value.GetString("output_file_id"))).size() ==
          1);
  for (const auto& line :
       Lines(Content(objects, final.value.GetString("error_file_id"))))
    Q4T_CHECK(line.Find("response")->IsNull() &&
              line.Find("error")->GetString("code") == "batch_cancelled");
  return true;
}

Q4T_TEST(
    batch_results_invalid_endpoint_json_and_ticket_do_not_complete_or_publish) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto ready = Ready(objects, 1);
  auto result = Start(batches, ready, 0);
  for (const auto* bad :
       {"[]", "{\"v\":1,\"v\":2}", "data: hello\n\n", "{\"v\":NaN}"}) {
    result.response_body = bad;
    Q4T_CHECK(
        batches
            .FinishRow(Access(), ready.id, 0, ready.rows[0].custom_id, result)
            .code == "batch_response_invalid");
  }
  result.response_body = "{}";
  result.http_status = 302;
  Q4T_CHECK(
      batches.FinishRow(Access(), ready.id, 0, ready.rows[0].custom_id, result)
          .code == "batch_response_invalid");
  const auto row = batches.Row(Access(), ready.id, 0);
  Q4T_CHECK(Ok(row) && row.value.GetString("status") == "running" &&
            row.value.Find("result_id")->IsNull());
  auto loaded = batches.Load(Access(), ready.id);
  Q4T_CHECK(Ok(loaded) && loaded.value.Find("data")->GetInt("next_row") == 0);
  result.http_status = 200;
  Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, 0, ready.rows[0].custom_id,
                                 result)));
  return true;
}

Q4T_TEST(batch_results_actual_unknown_start_never_grants_duplicate_execution) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  ReadyBatch ready;
  {
    ObjectStore objects(fixture.options);
    ready = Ready(objects, 1);
  }
  Injection injection("--q4t-object-worker-application-lost-ack");
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  auto started = batches.StartRow(Access(), ready.id, 0);
  Q4T_CHECK(!started.ok() && started.commit_unknown &&
            started.value.GetString("id") == ready.id);
  Drain(objects);
  auto row = batches.Row(Access(), ready.id, 0);
  Q4T_CHECK(Ok(row) && row.value.GetString("status") == "running" &&
            row.value.GetString("request_id") ==
                started.value.GetString("request_id") &&
            batches.StartRow(Access(), ready.id, 0).http_status == 409);
  return true;
}

Q4T_TEST(batch_results_actual_unknown_finish_commits_result_and_counts_once) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  ReadyBatch ready;
  BatchRowResult result;
  {
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    ready = Ready(objects, 1);
    result = Start(batches, ready, 0, 200, UsageBody());
  }
  {
    Injection injection("--q4t-object-worker-application-lost-ack");
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    auto finished = batches.FinishRow(Access(), ready.id, 0,
                                      ready.rows[0].custom_id, result);
    Q4T_CHECK(!finished.ok() && finished.commit_unknown &&
              finished.value.GetString("id") == ready.id);
    Drain(objects);
    auto row = batches.Row(Access(), ready.id, 0);
    auto root = batches.Load(Access(), ready.id);
    Q4T_CHECK(Ok(row) && Ok(root) &&
              row.value.GetString("status") == "completed" &&
              row.value.GetString("result_id") ==
                  finished.value.GetString("result_id") &&
              root.value.Find("data")->GetInt("next_row") == 1 &&
              root.value.Find("data")
                      ->Find("batch")
                      ->Find("request_counts")
                      ->GetInt("completed") == 1 &&
              root.value.Find("data")->Find("batch")->Find("usage") &&
              root.value.Find("data")->Find("batch")->Find("usage")->GetInt(
                  "total_tokens") == 14 &&
              batches.FinishRow(Access(), ready.id, 0, ready.rows[0].custom_id,
                                result)
                      .http_status == 409);
  }
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  Q4T_CHECK(Ok(batches.BeginFinalization(Access(), ready.id, "completed")));
  auto final = batches.Finalize(Access(), ready.id);
  Q4T_CHECK(
      Ok(final) && final.value.Find("usage")->GetInt("total_tokens") == 14 &&
      Lines(Content(objects, final.value.GetString("output_file_id"))).size() ==
          1);
  return true;
}

Q4T_TEST(
    batch_results_actual_unknown_finalization_publishes_both_files_and_terminal_atomically) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  ReadyBatch ready;
  {
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    ready = Ready(objects, 2);
    for (size_t i = 0; i < 2; ++i) {
      auto result = Start(batches, ready, i, i ? 400 : 200);
      Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, i,
                                     ready.rows[i].custom_id, result)));
    }
    Q4T_CHECK(Ok(batches.BeginFinalization(Access(), ready.id, "completed")));
  }
  Injection injection("--q4t-object-worker-application-lost-ack");
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  auto final = batches.Finalize(Access(), ready.id);
  Q4T_CHECK(!final.ok() && final.commit_unknown &&
            final.value.GetString("id") == ready.id);
  Drain(objects);
  auto retrieved = batches.Retrieve(Access(), ready.id);
  Q4T_CHECK(Ok(retrieved) &&
            retrieved.value.GetString("status") == "completed");
  for (const auto* key : {"output_file_id", "error_file_id"})
    Q4T_CHECK(final.value.GetString(key) == retrieved.value.GetString(key) &&
              Lines(Content(objects, retrieved.value.GetString(key))).size() ==
                  1);
  Q4T_CHECK(batches.Finalize(Access(), ready.id).http_status == 409);
  return true;
}

Q4T_TEST(
    batch_results_actual_dead_owner_started_row_is_interrupted_then_pending_can_continue) {
  Fixture fixture;
  int fds[2];
  Q4T_CHECK(pipe2(fds, O_CLOEXEC) == 0);
  const auto pid = fork();
  Q4T_CHECK(pid >= 0);
  if (pid == 0) {
    close(fds[0]);
    try {
      std::string id;
      {
        ObjectStore objects(fixture.options);
        BatchState batches(objects);
        auto ready = Ready(objects, 2);
        id = ready.id;
        Start(batches, ready, 0);
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
  const auto n = read(fds[0], bytes, sizeof(bytes));
  close(fds[0]);
  int status = 0;
  Q4T_CHECK(waitpid(pid, &status, 0) == pid && WIFEXITED(status) &&
            WEXITSTATUS(status) == 0 && n > 0);
  const std::string id(bytes, n);
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  Q4T_CHECK(batches.StartRow(Access(), id, 0).code == "batch_owner_changed");
  const auto before = batches.Retrieve(Access(), id);
  Q4T_CHECK(Ok(before) && Ok(batches.ClaimAbandoned(Access(), id)) &&
            batches.StartRow(Access(), id, 0).http_status == 409 &&
            batches.StartRow(Access(), id, 1).http_status == 409);
  auto row = batches.Row(Access(), id, 0);
  BatchInputRow input;
  Q4T_CHECK(Ok(row) && Ok(batches.ReadInputRow(Access(), id, 0, &input)));
  const auto* execution_owner = row.value.Find("execution_owner");
  Q4T_CHECK(execution_owner && ValidResponseOwner(*execution_owner) &&
            execution_owner->GetInt("pid") == pid &&
            ResponseOwnerStatus(*execution_owner) == ResponseOwnerState::kGone);
  BatchRowResult interrupted;
  interrupted.request_id = row.value.GetString("request_id");
  interrupted.error = Error(503, "batch_interrupted",
                            "Prior execution outcome unknown; not replayed");
  Q4T_CHECK(
      Ok(batches.FinishRow(Access(), id, 0, input.custom_id, interrupted)));
  auto started = batches.StartRow(Access(), id, 1);
  Q4T_CHECK(Ok(started) && Ok(batches.ReadInputRow(Access(), id, 1, &input)));
  BatchRowResult result{
      started.value.GetString("request_id"), 200, "{\"answer\":42}", {}};
  Q4T_CHECK(Ok(batches.FinishRow(Access(), id, 1, input.custom_id, result)) &&
            Ok(batches.BeginFinalization(Access(), id, "completed")));
  auto final = batches.Finalize(Access(), id);
  Q4T_CHECK(Ok(final) &&
            final.value.GetInt("expires_at") ==
                before.value.GetInt("expires_at") &&
            final.value.Find("request_counts")->GetInt("failed") == 1 &&
            final.value.Find("request_counts")->GetInt("completed") == 1);
  auto failures =
      Lines(Content(objects, final.value.GetString("error_file_id")));
  Q4T_CHECK(failures.size() == 1 && failures[0].Find("error")->GetString(
                                        "code") == "batch_interrupted");
  return true;
}

Q4T_TEST(
    batch_results_expired_immutable_deadline_produces_errors_without_start) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  auto ready = Ready(objects, 2);
  Q4T_CHECK(
      batches.BeginFinalization(Access(), ready.id, "expired").http_status ==
      409);
  // Construct a previous-day persisted record. Existing creation times are
  // immutable, so never edit a live record's original creation/deadline.
  auto stored = batches.Load(Access(), ready.id);
  Q4T_CHECK(Ok(stored));
  auto root = Request(NewObjectId(ObjectKind::kBatch));
  root.operation = ObjectOperation::kPut;
  root.created_at = std::time(nullptr) - kBatchWindowSeconds - 1;
  root.data = *stored.value.Find("data");
  auto& batch = Field(root.data, "batch");
  Set(&batch, "id", String(root.id));
  Set(&batch, "created_at", Number(root.created_at));
  Set(&batch, "expires_at", Number(root.created_at + kBatchWindowSeconds));
  auto old_page = objects.Execute(
      Request(root.data.GetArray("pages")->array[0].str, ObjectKind::kToolRun),
      Access().deadline);
  Q4T_CHECK(Ok(old_page));
  auto page = Request(NewObjectId(ObjectKind::kToolRun), ObjectKind::kToolRun);
  page.operation = ObjectOperation::kPut;
  page.ttl_seconds = 0;
  page.data = *old_page.value.Find("data");
  Set(&page.data, "batch_id", String(root.id));
  Set(&root.data, "pages", Array({String(page.id)}));
  const std::array mutations{page, root};
  Q4T_CHECK(Ok(objects.ExecuteBatch(mutations, Access().deadline)));
  ready.id = root.id;
  Q4T_CHECK(!batches.StartRow(Access(), ready.id, 0).ok());
  Q4T_CHECK(batches.Row(Access(), ready.id, 0).value.GetString("status") ==
                "pending" &&
            Ok(batches.BeginFinalization(Access(), ready.id, "expired")));
  for (size_t i = 0; i < 2; ++i) {
    BatchRowResult skipped;
    skipped.error =
        Error(408, "batch_expired", "Batch expired before request execution");
    Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, i,
                                   ready.rows[i].custom_id, skipped)));
  }
  auto final = batches.Finalize(Access(), ready.id);
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "expired" &&
            final.value.Find("output_file_id")->IsNull());
  for (const auto& line :
       Lines(Content(objects, final.value.GetString("error_file_id"))))
    Q4T_CHECK(line.Find("response")->IsNull() &&
              line.Find("error")->GetString("code") == "batch_expired");
  return true;
}

Q4T_TEST(
    batch_results_storage_quota_rejects_result_without_releasing_started_owner) {
  Fixture fixture;
  ReadyBatch ready;
  BatchRowResult result;
  int64_t count = 0;
  {
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    ready = Ready(objects, 1);
    result = Start(
        batches, ready, 0, 200,
        OutputJson(Object(
            {{"payload", String(std::string(kBatchInlineBytes + 1, 'r'))}})));
    auto health = Request("");
    health.operation = ObjectOperation::kHealth;
    count = objects.Execute(health, Access().deadline).value.GetInt("objects");
  }
  auto limited = fixture.options;
  limited.max_client_objects = count + 1;
  {
    ObjectStore objects(limited);
    BatchState batches(objects);
    auto failed = batches.FinishRow(Access(), ready.id, 0,
                                    ready.rows[0].custom_id, result);
    Q4T_CHECK(failed.code == "object_quota_exceeded" && !failed.commit_unknown);
    auto row = batches.Row(Access(), ready.id, 0),
         loaded = batches.Load(Access(), ready.id);
    Q4T_CHECK(Ok(row) && Ok(loaded) &&
              row.value.GetString("status") == "running" &&
              row.value.Find("result_id")->IsNull() &&
              loaded.value.Find("data")->GetInt("running") == 1 &&
              loaded.value.Find("data")->GetInt("next_row") == 0 &&
              batches.StartRow(Access(), ready.id, 0).http_status == 409);
  }
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  // Re-persist the already available result; this is not a model retry.
  Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, 0, ready.rows[0].custom_id,
                                 result)));
  return true;
}

Q4T_TEST(batch_results_finalization_quota_never_exposes_partial_files) {
  Fixture fixture;
  ReadyBatch ready;
  int64_t count = 0;
  {
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    ready = Ready(objects, 2);
    for (size_t i = 0; i < 2; ++i) {
      auto result = Start(batches, ready, i, i ? 400 : 200);
      Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, i,
                                     ready.rows[i].custom_id, result)));
    }
    Q4T_CHECK(Ok(batches.BeginFinalization(Access(), ready.id, "completed")));
    auto health = Request("");
    health.operation = ObjectOperation::kHealth;
    count = objects.Execute(health, Access().deadline).value.GetInt("objects");
  }
  auto limited = fixture.options;
  limited.max_client_objects = count + 1;
  {
    ObjectStore objects(limited);
    BatchState batches(objects);
    auto failed = batches.Finalize(Access(), ready.id);
    Q4T_CHECK(failed.code == "object_quota_exceeded" && !failed.commit_unknown);
    auto root = batches.Retrieve(Access(), ready.id);
    auto files = Files(objects).List(Access(), Object());
    Q4T_CHECK(Ok(root) && root.value.GetString("status") == "finalizing" &&
              root.value.Find("output_file_id")->IsNull() &&
              root.value.Find("error_file_id")->IsNull() && Ok(files) &&
              files.value.GetArray("data")->array.size() == 1);
  }
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  Q4T_CHECK(Ok(batches.Finalize(Access(), ready.id)));
  return true;
}

Q4T_TEST(batch_results_historical_receipt_does_not_pin_expired_output_bytes) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto ready = Ready(objects, 1);
  const auto result = Start(batches, ready, 0);
  Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, 0, ready.rows[0].custom_id,
                                 result)) &&
            Ok(batches.BeginFinalization(Access(), ready.id, "completed")));
  auto final = batches.Finalize(Access(), ready.id);
  Q4T_CHECK(Ok(final));
  const auto output = final.value.GetString("output_file_id");
  auto stored =
      objects.Execute(Request(output, ObjectKind::kFile), Access().deadline);
  Q4T_CHECK(Ok(stored));
  const auto chunk = stored.value.Find("data")
                         ->Find("blob")
                         ->GetArray("chunks")
                         ->array[0]
                         .GetString("id");
  int64_t expires = 0;
  Q4T_CHECK(
      Ok(Edit(objects, output, ObjectKind::kFile, [&](ObjectRequest& request) {
        // CPU clock fixture only: public output_expires_after still requires
        // >=1h.
        request.expires_at = request.created_at + 1;
        expires = request.expires_at;
        Set(&Field(request.data, "file"), "expires_at", Number(expires));
      })));
  const auto until = Clock::now() + 2s;
  while (std::time(nullptr) < expires && Clock::now() < until)
    std::this_thread::sleep_for(2ms);
  Q4T_CHECK(std::time(nullptr) >= expires &&
            Files(objects).Retrieve(Access(), output).http_status == 404 &&
            Ok(Collect(objects)));
  Q4T_CHECK(
      objects.Execute(Request(chunk, ObjectKind::kToolRun), Access().deadline)
              .http_status == 404 &&
      batches.Retrieve(Access(), ready.id).value.GetString("output_file_id") ==
          output);
  return true;
}

Q4T_TEST(batch_results_corrupt_page_blocks_reads_and_start_transactionally) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto ready = Ready(objects, 1);
  auto root = batches.Load(Access(), ready.id);
  Q4T_CHECK(Ok(root));
  const auto page = root.value.Find("data")->GetArray("pages")->array[0].str;
  Q4T_CHECK(
      Edit(objects, page, ObjectKind::kToolRun, [](ObjectRequest& request) {
        Set(&request.data, "batch_id", String("batch_other"));
      }).code == "batch_checkpoint_immutable");
  Q4T_CHECK(OutputJson(root.value) ==
            OutputJson(batches.Load(Access(), ready.id).value));
  BatchInputRow row;
  Q4T_CHECK(Ok(batches.ReadInputRow(Access(), ready.id, 0, &row)) &&
            row.custom_id == ready.rows[0].custom_id &&
            Ok(batches.StartRow(Access(), ready.id, 0)));
  return true;
}

Q4T_TEST(batch_inline_result_page_immutability_and_exact_number_retention) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  auto ready = Ready(objects, 1, 0, true);
  auto result = Start(batches, ready, 0, 200, "{\"exact\":1e-999}");
  Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, 0, ready.rows[0].custom_id,
                                 result)));
  const auto root = batches.Load(Access(), ready.id);
  const auto page_id = root.value.Find("data")->GetArray("pages")->array[0].str;
  const auto row = batches.Row(Access(), ready.id, 0);
  Q4T_CHECK(
      Ok(row) && ValidBatchInlineResult(row.value) &&
      row.value.Find("result_inline")->GetString("payload").find("1e-999") !=
          std::string::npos);
  for (int which = 0; which < 4; ++which) {
    auto changed = Edit(
        objects, page_id, ObjectKind::kToolRun, [&](ObjectRequest& request) {
          auto& value = Field(request.data, "rows").array[0];
          if (which == 0) {
            auto& embedded = Field(value, "result_inline");
            auto payload = Parsed(embedded.GetString("payload"));
            Set(&Field(Field(payload, "response"), "body"), "exact",
                Number(99));
            const auto text = OutputJson(payload) + '\n';
            Set(&embedded, "payload", String(text));
            Set(&embedded, "sha256", String(Digest(text)));
          }
          if (which == 1) Set(&value, "finished_at", Number(0));
          if (which == 2) Set(&value, "status", String("running"));
          if (which == 3)
            Set(&value, "result_id", String(NewObjectId(ObjectKind::kToolRun)));
        });
    Q4T_CHECK(changed.code == "batch_checkpoint_immutable");
  }
  Q4T_CHECK(Ok(batches.BeginFinalization(Access(), ready.id, "completed")));
  auto final = batches.Finalize(Access(), ready.id);
  Q4T_CHECK(Ok(final) &&
            Content(objects, final.value.GetString("output_file_id")) ==
                row.value.Find("result_inline")->GetString("payload"));
  return true;
}

Q4T_TEST(batch_finalization_row_budget_and_checkpoint_restart) {
  Fixture fixture;
  ReadyBatch ready;
  int64_t expires = 0;
  {
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    ready = Ready(objects, kBatchPageRows + 1, 0, true);
    for (size_t i = 0; i < ready.rows.size(); ++i) {
      auto result = Start(batches, ready, i);
      Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, i,
                                     ready.rows[i].custom_id, result)));
    }
    Q4T_CHECK(Ok(batches.BeginFinalization(Access(), ready.id, "completed")));
    auto step = batches.Finalize(Access(), ready.id);
    Q4T_CHECK(Ok(step) && step.value.GetString("status") == "finalizing" &&
              step.value.Find("output_file_id")->IsNull());
    auto root = batches.Load(Access(), ready.id);
    expires = root.value.GetInt("expires_at");
    const auto* progress = root.value.Find("data")->Find("finalization");
    Q4T_CHECK(progress && progress->GetInt("next_row") == kBatchPageRows &&
              progress->GetInt("source_chunk") == 0 &&
              progress->Find("output")->GetInt("bytes") > 0 &&
              Ok(Files(objects).Erase(Access(), ready.file)) &&
              Ok(Collect(objects)));
    auto files = Files(objects).List(Access(), Object());
    Q4T_CHECK(Ok(files) && files.value.GetArray("data")->array.empty());
  }
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  Q4T_CHECK(batches.Load(Access(), ready.id).value.GetInt("expires_at") ==
            expires);
  auto final = batches.Finalize(Access(), ready.id);
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "completed");
  const auto lines =
      Lines(Content(objects, final.value.GetString("output_file_id")));
  Q4T_CHECK(lines.size() == ready.rows.size());
  for (size_t i = 0; i < lines.size(); ++i)
    Q4T_CHECK(lines[i].GetString("custom_id") == ready.rows[i].custom_id);
  return true;
}

Q4T_TEST(batch_finalization_large_row_lost_checkpoint_resumes_original_bytes) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  fixture.options.max_client_bytes = 256ULL << 20;
  fixture.options.max_total_bytes = 512ULL << 20;
  ReadyBatch ready;
  const auto body = OutputJson(
      Object({{"large",
               String(std::string(9 * kBinaryChunkBytes + 17, 'x') + "42")}}));
  {
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    ready = Ready(objects, 1);
    auto result = Start(batches, ready, 0, 200, body);
    Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, 0,
                                   ready.rows[0].custom_id, result)) &&
              Ok(batches.BeginFinalization(Access(), ready.id, "completed")));
  }
  {
    Injection injection("--q4t-object-worker-application-lost-ack");
    ObjectStore objects(fixture.options);
    auto step = BatchState(objects).Finalize(Access(), ready.id);
    Q4T_CHECK(!step.ok() && step.commit_unknown &&
              step.value.Find("output_file_id")->IsNull());
    Drain(objects);
    auto root = BatchState(objects).Load(Access(), ready.id);
    Q4T_CHECK(Ok(root));
    const auto* progress = root.value.Find("data")->Find("finalization");
    Q4T_CHECK(progress && progress->GetInt("next_row") == 0 &&
              progress->GetInt("source_chunk") == 4 &&
              progress->GetInt("completed") == 0 &&
              progress->Find("output")->GetInt("bytes") ==
                  4 * kBinaryChunkBytes);
  }
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  Q4T_CHECK(Ok(Collect(objects)));
  auto step = batches.Finalize(Access(), ready.id);
  Q4T_CHECK(Ok(step) && step.value.GetString("status") == "finalizing");
  auto root = batches.Load(Access(), ready.id);
  Q4T_CHECK(
      root.value.Find("data")->Find("finalization")->GetInt("source_chunk") ==
      8);
  auto final = batches.Finalize(Access(), ready.id);
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "completed");
  const auto lines =
      Lines(Content(objects, final.value.GetString("output_file_id")));
  Q4T_CHECK(lines.size() == 1 &&
            lines[0].GetString("custom_id") == ready.rows[0].custom_id &&
            OutputJson(*lines[0].Find("response")->Find("body")) == body);
  return true;
}

Q4T_TEST(batch_finalization_actual_dead_process_keeps_committed_prefix) {
  Fixture fixture;
  int fds[2];
  Q4T_CHECK(pipe2(fds, O_CLOEXEC) == 0);
  const auto pid = fork();
  Q4T_CHECK(pid >= 0);
  if (pid == 0) {
    close(fds[0]);
    try {
      std::string id;
      {
        ObjectStore objects(fixture.options);
        BatchState batches(objects);
        auto ready = Ready(objects, 129, 0, true);
        id = ready.id;
        for (size_t i = 0; i < ready.rows.size(); ++i) {
          const auto result = Start(batches, ready, i, i % 2 ? 400 : 200);
          if (!Ok(batches.FinishRow(Access(), id, i, ready.rows[i].custom_id,
                                    result)))
            _exit(4);
        }
        if (!Ok(batches.BeginFinalization(Access(), id, "completed")) ||
            !Ok(batches.Finalize(Access(), id)))
          _exit(5);
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
  const auto n = read(fds[0], bytes, sizeof(bytes));
  close(fds[0]);
  int status = 0;
  Q4T_CHECK(waitpid(pid, &status, 0) == pid && WIFEXITED(status) &&
            WEXITSTATUS(status) == 0 && n > 0);
  const std::string id(bytes, n);
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto before = batches.Load(Access(), id);
  Q4T_CHECK(
      Ok(before) &&
      before.value.Find("data")->Find("finalization")->GetInt("next_row") ==
          128 &&
      batches.Finalize(Access(), id).code == "batch_owner_changed" &&
      Ok(batches.ClaimAbandoned(Access(), id)));
  auto final = batches.Finalize(Access(), id);
  Q4T_CHECK(
      Ok(final) && final.value.GetString("status") == "completed" &&
      Lines(Content(objects, final.value.GetString("output_file_id"))).size() ==
          65 &&
      Lines(Content(objects, final.value.GetString("error_file_id"))).size() ==
          64);
  return true;
}

Q4T_TEST(
    batch_finalization_maximum_dual_lane_retention_is_bounded_and_complete) {
  BinaryManifest lane;
  for (size_t i = 0; i < kBinaryMaxChunks; ++i)
    lane.chunks.push_back(
        {"tool_run_" + std::string(40, 'a') + std::to_string(i),
         Digest("fixture chunk"), kBinaryMaxBytes / kBinaryMaxChunks});
  lane.bytes = kBinaryMaxBytes;
  auto other = lane;
  for (auto& chunk : other.chunks) chunk.id[9] = 'b';
  auto pages = Array();
  for (size_t i = 0; i < kBatchPages; ++i)
    pages.array.push_back(String("tool_run_page_" + std::to_string(i)));
  auto data = Object(
      {{"format", String("q4t.batch.v1")},
       {"batch", Object({{"status", String("finalizing")}})},
       {"blob", BinaryManifestJson({})},
       {"pages", pages},
       {"finalization", Object({{"format", String("q4t.batch_finalization.v1")},
                                {"output", BinaryManifestJson(lane)},
                                {"error", BinaryManifestJson(other)}})}});
  const auto retained = InspectObjectRetention("batch", data);
  Q4T_CHECK(retained.active && !retained.uncertain &&
            retained.references.size() == 2 * kBinaryMaxChunks + kBatchPages);
  Set(&data, "resources_released", Boolean(true));
  Set(&Field(data, "batch"), "status", String("completed"));
  const auto released = InspectObjectRetention("batch", data);
  Q4T_CHECK(!released.active && !released.uncertain &&
            released.references.empty());
  return true;
}

Q4T_TEST(batch_capacity_actual_fifty_thousand_frozen_rows_and_outputs) {
  Fixture fixture;
  fixture.options.max_client_bytes = 768ULL << 20;
  fixture.options.max_total_bytes = 1ULL << 30;
  fixture.options.max_client_objects = 1500;
  fixture.options.max_total_objects = 1500;
  fixture.options.io_timeout_ms = 2000;
  auto objects = std::make_unique<ObjectStore>(fixture.options);
  auto ready = Ready(*objects, kBatchRequests, 0, true);
  auto loaded = BatchState(*objects).Load(Access(), ready.id);
  Q4T_CHECK(Ok(loaded));
  const auto pages = *loaded.value.Find("data")->GetArray("pages");
  Q4T_CHECK(pages.array.size() == kBatchPages);
  // Capacity fixture seeds explicit CPU endpoint receipts per page. The
  // native Start/Finish lifecycle is exercised separately across 129 rows;
  // this test proves actual storage/aggregation of all 50K prepared requests,
  // not model execution or a synthetic arithmetic estimate of object count.
  std::array<std::string, 2> expected;
  size_t index = 0;
  for (const auto& page : pages.array) {
    auto written = Edit(
        *objects, page.str, ObjectKind::kToolRun, [&](ObjectRequest& request) {
          for (auto& row : Field(request.data, "rows").array) {
            const size_t lane = index % 3 == 0 ? 1 : 0;
            const auto ticket = "req_capacity_" + std::to_string(index);
            const auto text =
                OutputJson(Object(
                    {{"id",
                      String("batch_req_capacity_" + std::to_string(index))},
                     {"custom_id", String(ready.rows[index].custom_id)},
                     {"response",
                      Object({{"status_code", Number(lane ? 400 : 200)},
                              {"request_id", String(ticket)},
                              {"body", Object({{"cpu_capacity_fixture",
                                                Number(index)}})}})},
                     {"error", Json()}})) +
                '\n';
            expected[lane] += text;
            Set(&row, "status", String(lane ? "failed" : "completed"));
            Set(&row, "request_id", String(ticket));
            Set(&row, "result_inline",
                Object({{"sha256", String(Digest(text))},
                        {"payload", String(text)}}));
            ++index;
          }
        });
    Q4T_CHECK(Ok(written));
  }
  const int64_t failed = (kBatchRequests + 2) / 3;
  Q4T_CHECK(index == kBatchRequests &&
            Ok(Edit(*objects, ready.id, ObjectKind::kBatch,
                    [&](ObjectRequest& request) {
                      Set(&request.data, "next_row", Number(kBatchRequests));
                      auto& counts =
                          Field(Field(request.data, "batch"), "request_counts");
                      Set(&counts, "completed",
                          Number(kBatchRequests - failed));
                      Set(&counts, "failed", Number(failed));
                    })));
  BatchPreparation prepare(*objects);
  for (size_t at : {size_t{0}, kBatchRequests - 1}) {
    auto row = BatchState(*objects).Row(Access(), ready.id, at);
    Json frozen;
    Q4T_CHECK(Ok(row) && Ok(prepare.Read(Access(), ready.id, "/v1/responses",
                                         ready.rows[at], row.value, &frozen)));
    Q4T_CHECK(frozen.Find("request")->GetString("input") ==
              ready.rows[at].body.GetString("input"));
  }
  Q4T_CHECK(Ok(
      BatchState(*objects).BeginFinalization(Access(), ready.id, "completed")));
  ObjectReply final;
  for (size_t turn = 0; turn < kBatchPages; ++turn) {
    if (turn == 10) {
      objects.reset();
      objects = std::make_unique<ObjectStore>(fixture.options);
    }
    final = BatchState(*objects).Finalize(Access(), ready.id);
    Q4T_CHECK(Ok(final));
    if (turn + 1 != kBatchPages)
      Q4T_CHECK(final.value.GetString("status") == "finalizing" &&
                final.value.Find("output_file_id")->IsNull());
  }
  Q4T_CHECK(final.value.GetString("status") == "completed");
  for (size_t lane = 0; lane < 2; ++lane) {
    const auto content = Content(
        *objects,
        final.value.GetString(lane ? "error_file_id" : "output_file_id"));
    Q4T_CHECK(content.size() == expected[lane].size() &&
              Digest(content) == Digest(expected[lane]));
  }
  auto health = Request("");
  health.operation = ObjectOperation::kHealth;
  const auto usage = objects->Execute(health, Access().deadline);
  Q4T_CHECK(Ok(usage) && usage.value.GetInt("objects") < 1500);
  std::fprintf(
      stdout, "  actual 50000 rows, %zu finalization turns, %lld objects\n",
      kBatchPages, static_cast<long long>(usage.value.GetInt("objects")));
  return true;
}

Q4T_TEST(batch_captured_usage_survives_inline_binary_recount_and_restart) {
  Fixture fixture;
  ReadyBatch ready;
  std::array<std::string, 3> original;
  {
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    ready = Ready(objects, 3);
    for (size_t i = 0; i < 3; ++i) {
      auto result = Start(
          batches, ready, i, 502,
          OutputJson(Object(
              {{"error",
                Object({{"code", String("failed")},
                        {"message", String(std::string(i == 1 ? (2 << 20) : 8,
                                                       'x'))}})}})));
      if (i == 2) {
        result.response_body.clear();
        result.error = Error(499, "batch_cancelled", "Drained cancellation");
      }
      result.actual_usage = ActualUsage{8, 3, 2, 1, 0};
      Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, i,
                                     ready.rows[i].custom_id, result)));
      const auto row = batches.Row(Access(), ready.id, i);
      Q4T_CHECK(Ok(row) && row.value.Find("actual_usage") &&
                row.value.Find("actual_usage")->GetInt("total_tokens") == 11);
      Q4T_CHECK(row.value.Has("result_inline") == (i != 1));
      original[i] = OutputJson(row.value);
    }
    Q4T_CHECK(Ok(LegacyUsage(objects, ready.id)));
    Q4T_CHECK(Ok(batches.RecountUsage(Access(), ready.id)));
  }
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  Q4T_CHECK(Ok(batches.RecountUsage(Access(), ready.id)) &&
            Ok(batches.RecountUsage(Access(), ready.id)));
  for (size_t i = 0; i < 3; ++i)
    Q4T_CHECK(OutputJson(batches.Row(Access(), ready.id, i).value) ==
              original[i]);
  Q4T_CHECK(Ok(batches.BeginFinalization(Access(), ready.id, "completed")));
  const auto final = batches.Finalize(Access(), ready.id);
  Q4T_CHECK(Ok(final) && final.value.Find("usage") &&
            OutputJson(*final.value.Find("usage")) ==
                OutputJson(BatchUsageJson({24, 9, 6, 3})));
  const auto text = Content(objects, final.value.GetString("error_file_id"));
  Q4T_CHECK(text.find("actual_usage") == text.npos);
  const auto lines = Lines(text);
  Q4T_CHECK(lines.size() == 3 && lines[2].Find("response")->IsNull());
  for (size_t i = 0; i < 2; ++i)
    Q4T_CHECK(!lines[i].Find("response")->Find("body")->Has("usage"));
  return true;
}

Q4T_TEST(batch_captured_usage_is_immutable_and_not_client_input) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  const auto ready = Ready(objects, 1);
  auto result =
      Start(batches, ready, 0, 500,
            OutputJson(Object({{"error", String(std::string(16384, 'x'))}})));
  const auto root = batches.Load(Access(), ready.id);
  const auto page_id = root.value.Find("data")->GetArray("pages")->array[0].str;
  const auto premature =
      Edit(objects, page_id, ObjectKind::kToolRun, [](ObjectRequest& page) {
        Set(&Field(page.data, "rows").array[0], "actual_usage",
            BatchUsageJson({8, 3, 2, 1}));
      });
  Q4T_CHECK(premature.code == "batch_checkpoint_immutable");
  result.actual_usage = ActualUsage{8, 3, 2, 1, 0};
  Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, 0, ready.rows[0].custom_id,
                                 result)));
  for (int mode : {0, 1, 2}) {
    const auto changed =
        Edit(objects, page_id, ObjectKind::kToolRun, [&](ObjectRequest& page) {
          auto& row = Field(page.data, "rows").array[0];
          if (mode == 0) Set(&row, "actual_usage", BatchUsageJson({}));
          if (mode == 1)
            std::erase_if(row.object, [](const auto& field) {
              return field.first == "actual_usage";
            });
          if (mode == 2)
            Set(&Field(row, "actual_usage"), "input_tokens",
                *Parsed("{\"n\":1e20}").Find("n"));
        });
    Q4T_CHECK(changed.code == "batch_checkpoint_immutable");
  }
  Q4T_CHECK(batches.Row(Access("client:other"), ready.id, 0).http_status ==
            404);
  return true;
}

Q4T_TEST(batch_captured_usage_does_not_override_reported_or_invalid_usage) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  for (const int mode : {0, 1, 2}) {
    const auto ready = Ready(objects, 1);
    auto result = Start(batches, ready, 0, 500,
                        mode == 0   ? UsageBody()
                        : mode == 1 ? "{\"usage\":{\"input_tokens\":1e20}}"
                                    : "{\"error\":{}}");
    result.actual_usage = mode == 2
                              ? ActualUsage{kActualUsageLimit + 1, 0, 0, 0, 0}
                              : ActualUsage{8, 3, 2, 1, 0};
    Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, 0,
                                   ready.rows[0].custom_id, result)));
    Q4T_CHECK(!batches.Row(Access(), ready.id, 0).value.Has("actual_usage"));
    const auto batch = batches.Retrieve(Access(), ready.id);
    Q4T_CHECK(Ok(batch) && batch.value.Has("usage") == (mode == 0));
    if (mode == 0)
      Q4T_CHECK(batch.value.Find("usage")->GetInt("total_tokens") == 14);
  }
  return true;
}

Q4T_TEST(batch_captured_usage_actual_lost_finish_ack_never_recharges) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  ReadyBatch ready;
  BatchRowResult result;
  {
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    ready = Ready(objects, 1);
    result = Start(batches, ready, 0, 500, "{\"error\":{\"code\":\"failed\"}}");
    result.actual_usage = ActualUsage{8, 3, 2, 1, 0};
  }
  {
    Injection injection("--q4t-object-worker-application-lost-ack");
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    const auto missing = batches.FinishRow(Access(), ready.id, 0,
                                           ready.rows[0].custom_id, result);
    Q4T_CHECK(!missing.ok() && missing.commit_unknown);
    Drain(objects);
  }
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  Q4T_CHECK(
      batches.StartRow(Access(), ready.id, 0).http_status == 409 &&
      batches.FinishRow(Access(), ready.id, 0, ready.rows[0].custom_id, result)
              .http_status == 409);
  const auto row = batches.Row(Access(), ready.id, 0);
  Q4T_CHECK(Ok(row) && row.value.Find("actual_usage") &&
            row.value.Find("actual_usage")->GetInt("total_tokens") == 11);
  Q4T_CHECK(Ok(LegacyUsage(objects, ready.id)) &&
            Ok(batches.RecountUsage(Access(), ready.id)));
  Q4T_CHECK(batches.Retrieve(Access(), ready.id)
                .value.Find("usage")
                ->GetInt("total_tokens") == 11);
  return true;
}

namespace {
ObjectReply FinishOutput(BatchState& batches, const std::string& id,
                         size_t max_turns = 16) {
  ObjectReply result;
  for (size_t turn = 0; turn < max_turns; ++turn) {
    result = batches.Finalize(Access(), id);
    if (!result.ok() || BatchTerminal(result.value.GetString("status")))
      return result;
  }
  return Error(500, "test_finalization_not_bounded", "No finite terminal");
}
}  // namespace

Q4T_TEST(batch_output_byte_failure_publishes_whole_dual_lane_prefix_and_usage) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects, {2048, kBinaryMaxChunks});
  const auto ready = Ready(objects, 3);
  std::array<std::string, 3> originals;
  for (size_t i = 0; i < 3; ++i) {
    auto result = Start(batches, ready, i, i == 1 ? 400 : 200,
                        UsageBody(10, 4, 3, 2, i == 2 ? 8192 : 0));
    Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, i,
                                   ready.rows[i].custom_id, result)));
    originals[i] = OutputJson(batches.Row(Access(), ready.id, i).value);
  }
  Q4T_CHECK(Ok(batches.BeginFinalization(Access(), ready.id, "completed")));
  const auto final = FinishOutput(batches, ready.id);
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "failed" &&
            final.value.Find("usage")->GetInt("total_tokens") == 42);
  const auto* errors = final.value.Find("errors")->GetArray("data");
  Q4T_CHECK(errors && errors->array.size() == 1 &&
            errors->array[0].GetString("code") ==
                "batch_output_capacity_exceeded" &&
            errors->array[0].GetInt("line") == 3);
  const auto output =
      Lines(Content(objects, final.value.GetString("output_file_id")));
  const auto error =
      Lines(Content(objects, final.value.GetString("error_file_id")));
  Q4T_CHECK(output.size() == 1 && error.size() == 1 &&
            output[0].GetString("custom_id") == ready.rows[0].custom_id &&
            error[0].GetString("custom_id") == ready.rows[1].custom_id);
  const auto root = batches.Load(Access(), ready.id);
  Q4T_CHECK(Ok(root) && root.value.Find("data")->GetBool("resources_released"));
  Q4T_CHECK(Ok(Collect(objects)));
  for (size_t i = 0; i < 3; ++i)
    Q4T_CHECK(OutputJson(batches.Row(Access(), ready.id, i).value) ==
              originals[i]);
  Q4T_CHECK(batches.StartRow(Access(), ready.id, 0).http_status == 409 &&
            batches.Retrieve(Access("client:other"), ready.id).http_status ==
                404);
  return true;
}

Q4T_TEST(batch_output_chunk_failure_rebuilds_committed_whole_page_prefix) {
  Fixture fixture;
  ReadyBatch ready;
  {
    ObjectStore objects(fixture.options);
    BatchState batches(objects, {kBinaryMaxBytes, 1});
    ready = Ready(objects, kBatchPageRows + 1);
    for (size_t i = 0; i < ready.rows.size(); ++i) {
      const auto result = Start(batches, ready, i, 200, UsageBody());
      Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, i,
                                     ready.rows[i].custom_id, result)));
    }
    Q4T_CHECK(Ok(batches.BeginFinalization(Access(), ready.id, "completed")));
    Q4T_CHECK(Ok(batches.Finalize(Access(), ready.id)));
    const auto prefix = batches.Load(Access(), ready.id);
    Q4T_CHECK(
        prefix.value.Find("data")->Find("finalization")->GetInt("next_row") ==
        kBatchPageRows);
    Q4T_CHECK(Ok(batches.Finalize(Access(), ready.id)));
    const auto stopped = batches.Load(Access(), ready.id);
    Q4T_CHECK(stopped.value.Find("data")
                      ->Find("output_failure")
                      ->GetInt("row_stop") == kBatchPageRows &&
              !stopped.value.Find("data")->Has("finalization"));
  }
  ObjectStore objects(fixture.options);
  BatchState batches(objects, {kBinaryMaxBytes, 1});
  const auto final = FinishOutput(batches, ready.id);
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "failed");
  const auto lines =
      Lines(Content(objects, final.value.GetString("output_file_id")));
  Q4T_CHECK(lines.size() == kBatchPageRows &&
            final.value.Find("request_counts")->GetInt("completed") ==
                kBatchPageRows + 1 &&
            final.value.Find("usage")->GetInt("total_tokens") ==
                14 * (kBatchPageRows + 1));
  for (size_t i = 0; i < lines.size(); ++i)
    Q4T_CHECK(lines[i].GetString("custom_id") == ready.rows[i].custom_id);
  return true;
}

Q4T_TEST(
    batch_output_legacy_partial_row_is_removed_before_failed_file_publish) {
  Fixture fixture;
  fixture.options.max_client_bytes = 256ULL << 20;
  fixture.options.max_total_bytes = 512ULL << 20;
  ReadyBatch ready;
  {
    ObjectStore objects(fixture.options);
    BatchState batches(objects);
    ready = Ready(objects, 2);
    for (size_t i = 0; i < 2; ++i) {
      const auto result = Start(batches, ready, i, 200,
                                UsageBody(10, 4, 3, 2, i ? (6 << 20) : 0));
      Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, i,
                                     ready.rows[i].custom_id, result)));
    }
    Q4T_CHECK(Ok(batches.BeginFinalization(Access(), ready.id, "completed")) &&
              Ok(batches.Finalize(Access(), ready.id)));
    const auto root = batches.Load(Access(), ready.id);
    const auto* progress = root.value.Find("data")->Find("finalization");
    Q4T_CHECK(progress && progress->GetInt("next_row") == 1 &&
              progress->GetInt("source_chunk") > 0);
  }
  ObjectStore objects(fixture.options);
  BatchState limited(objects, {4ULL << 20, kBinaryMaxChunks});
  const auto final = FinishOutput(limited, ready.id);
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "failed");
  const auto text = Content(objects, final.value.GetString("output_file_id"));
  const auto lines = Lines(text);
  Q4T_CHECK(lines.size() == 1 && text.back() == '\n' &&
            lines[0].GetString("custom_id") == ready.rows[0].custom_id &&
            final.value.Find("usage")->GetInt("total_tokens") == 28 &&
            Ok(Collect(objects)) && Ok(limited.Row(Access(), ready.id, 1)));
  return true;
}

Q4T_TEST(batch_output_failure_lost_checkpoint_and_terminal_receipts_reconcile) {
  for (const bool terminal : {false, true}) {
    Fixture fixture;
    fixture.options.io_timeout_ms = 250;
    ReadyBatch ready;
    {
      ObjectStore objects(fixture.options);
      BatchState batches(objects, {2048, kBinaryMaxChunks});
      ready = Ready(objects, 2);
      for (size_t i = 0; i < 2; ++i) {
        const auto result =
            Start(batches, ready, i, 200, UsageBody(10, 4, 3, 2, i ? 8192 : 0));
        Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, i,
                                       ready.rows[i].custom_id, result)));
      }
      Q4T_CHECK(Ok(batches.BeginFinalization(Access(), ready.id, "completed")));
      if (terminal) Q4T_CHECK(Ok(batches.Finalize(Access(), ready.id)));
    }
    {
      Injection injection("--q4t-object-worker-application-lost-ack");
      ObjectStore objects(fixture.options);
      BatchState batches(objects, {2048, kBinaryMaxChunks});
      const auto missing = batches.Finalize(Access(), ready.id);
      Q4T_CHECK(!missing.ok() && missing.commit_unknown);
      Drain(objects);
    }
    ObjectStore objects(fixture.options);
    BatchState batches(objects, {2048, kBinaryMaxChunks});
    auto final = batches.Retrieve(Access(), ready.id);
    Q4T_CHECK(Ok(final));
    if (!terminal) {
      Q4T_CHECK(final.value.GetString("status") == "finalizing");
      final = FinishOutput(batches, ready.id);
    }
    Q4T_CHECK(Ok(final) && final.value.GetString("status") == "failed" &&
              final.value.Find("usage")->GetInt("total_tokens") == 28 &&
              Lines(Content(objects, final.value.GetString("output_file_id")))
                      .size() == 1);
    Q4T_CHECK(batches.StartRow(Access(), ready.id, 0).http_status == 409 &&
              Files(objects)
                      .List(Access(), Object())
                      .value.GetArray("data")
                      ->array.size() == 2);
  }
  return true;
}

Q4T_TEST(batch_output_failure_original_results_have_real_finite_retention) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects, {1, kBinaryMaxChunks});
  const auto ready = Ready(objects, 1);
  const auto result =
      Start(batches, ready, 0, 200, UsageBody(10, 4, 3, 2, 16384));
  Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, 0, ready.rows[0].custom_id,
                                 result)) &&
            Ok(batches.BeginFinalization(Access(), ready.id, "completed")) &&
            Ok(batches.Finalize(Access(), ready.id)));
  const auto row = batches.Row(Access(), ready.id, 0);
  Q4T_CHECK(Ok(row));
  const auto result_id = row.value.GetString("result_id");
  const int64_t expires = std::time(nullptr) + 2;
  // Trusted historical terminal fixture: original 1h policy remains intact.
  // This is the first terminal transition, never a rewrite of a past result.
  Q4T_CHECK(
      Ok(Edit(objects, ready.id, ObjectKind::kBatch, [&](ObjectRequest& root) {
        auto& batch = Field(root.data, "batch");
        Set(&batch, "status", String("failed"));
        Set(&batch, "failed_at", Number(expires - 3600));
        Set(&batch, "errors",
            Object(
                {{"object", String("list")},
                 {"data",
                  Array({Object(
                      {{"code", String("batch_output_capacity_exceeded")},
                       {"message", String("CPU historical capacity terminal")},
                       {"line", Number(1)},
                       {"param", Json()}})})}}));
        Set(&root.data, "resources_released", Boolean(true));
        Set(&root.data, "output_failure",
            BatchOutputFailureJson({0, "bytes", expires}));
      })));
  Q4T_CHECK(Ok(Files(objects).Erase(Access(), ready.file)) &&
            Ok(Collect(objects)) && Ok(batches.Row(Access(), ready.id, 0)) &&
            batches.Row(Access("client:other"), ready.id, 0).http_status ==
                404 &&
            Ok(objects.Execute(Request(result_id, ObjectKind::kToolRun),
                               Access().deadline)));
  BatchInputRow original_input;
  Q4T_CHECK(batches.ReadInputRow(Access(), ready.id, 0, &original_input)
                    .http_status == 409 &&
            batches.StartRow(Access(), ready.id, 0).http_status == 409);
  const auto until = Clock::now() + 3s;
  while (std::time(nullptr) < expires && Clock::now() < until)
    std::this_thread::sleep_for(2ms);
  Q4T_CHECK(std::time(nullptr) >= expires &&
            batches.Row(Access(), ready.id, 0).http_status == 409 &&
            Ok(Collect(objects)) &&
            objects.Execute(Request(result_id, ObjectKind::kToolRun),
                            Access().deadline)
                    .http_status == 404 &&
            batches.Retrieve(Access(), ready.id).value.GetString("status") ==
                "failed");
  return true;
}

Q4T_TEST(batch_output_failure_checkpoint_cannot_expand_or_fake_a_terminal) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BatchState batches(objects, {1, kBinaryMaxChunks});
  const auto ready = Ready(objects, 2);
  for (size_t i = 0; i < 2; ++i) {
    const auto result = Start(batches, ready, i);
    Q4T_CHECK(Ok(batches.FinishRow(Access(), ready.id, i,
                                   ready.rows[i].custom_id, result)));
  }
  Q4T_CHECK(Ok(batches.BeginFinalization(Access(), ready.id, "completed")) &&
            Ok(batches.Finalize(Access(), ready.id)));
  for (const int mode : {0, 1, 2, 3}) {
    const auto rejected =
        Edit(objects, ready.id, ObjectKind::kBatch, [&](ObjectRequest& root) {
          auto& failure = Field(root.data, "output_failure");
          if (mode == 0) Set(&failure, "row_stop", Number(1));
          if (mode == 1)
            Set(&failure, "row_stop", *Parsed("{\"n\":1e20}").Find("n"));
          if (mode == 2)
            Set(&failure, "retain_until", Number(std::time(nullptr) + 3600));
          if (mode == 3)
            Set(&Field(root.data, "batch"), "status", String("completed"));
        });
    Q4T_CHECK(!rejected.ok() && !rejected.commit_unknown);
  }
  auto cancel = batches.Cancel(Access(), ready.id);
  Q4T_CHECK(Ok(cancel) && cancel.value.GetString("status") == "finalizing");
  const auto final = FinishOutput(batches, ready.id);
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "failed" &&
            final.value.Find("output_file_id")->IsNull() &&
            final.value.Find("error_file_id")->IsNull());
  Q4T_CHECK(BatchState(objects, {0, 1}).Finalize(Access(), ready.id).code ==
            "batch_output_limits_invalid");
  return true;
}

Q4T_TEST(batch_output_failure_actual_dead_owner_resumes_original_results) {
  Fixture fixture;
  int fds[2];
  Q4T_CHECK(pipe2(fds, O_CLOEXEC) == 0);
  const auto pid = fork();
  Q4T_CHECK(pid >= 0);
  if (pid == 0) {
    close(fds[0]);
    try {
      std::string id;
      {
        ObjectStore objects(fixture.options);
        BatchState batches(objects, {2048, kBinaryMaxChunks});
        const auto ready = Ready(objects, 2);
        id = ready.id;
        for (size_t i = 0; i < 2; ++i) {
          const auto result = Start(batches, ready, i, 200,
                                    UsageBody(10, 4, 3, 2, i ? 8192 : 0));
          if (!Ok(batches.FinishRow(Access(), id, i, ready.rows[i].custom_id,
                                    result)))
            _exit(4);
        }
        if (!Ok(batches.BeginFinalization(Access(), id, "completed")) ||
            !Ok(batches.Finalize(Access(), id)))
          _exit(5);
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
  char buffer[128];
  const auto size = read(fds[0], buffer, sizeof(buffer));
  close(fds[0]);
  int status = 0;
  Q4T_CHECK(waitpid(pid, &status, 0) == pid && WIFEXITED(status) &&
            WEXITSTATUS(status) == 0 && size > 0);
  const std::string id(buffer, size);
  ObjectStore objects(fixture.options);
  BatchState batches(objects, {2048, kBinaryMaxChunks});
  Q4T_CHECK(batches.Finalize(Access(), id).code == "batch_owner_changed" &&
            Ok(batches.ClaimAbandoned(Access(), id)));
  const auto final = FinishOutput(batches, id);
  Q4T_CHECK(
      Ok(final) && final.value.GetString("status") == "failed" &&
      final.value.Find("usage")->GetInt("total_tokens") == 28 &&
      Lines(Content(objects, final.value.GetString("output_file_id"))).size() ==
          1 &&
      batches.StartRow(Access(), id, 0).http_status == 409);
  return true;
}
