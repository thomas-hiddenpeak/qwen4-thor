#include "q4t/server/batch_preparation.h"
#include "q4t/server/batch_validation.h"
#include "q4t/server/batch_checkpoint.h"
#include "q4t/server/batch_quota.h"

#include <algorithm>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/chat_file_inputs.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/files.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/moderations_plan.h"
#include "q4t/server/embeddings_plan.h"
#include "q4t/server/embedding_metadata.h"
#include "q4t/server/object_retention.h"
#include "q4t/server/responses_execution.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
struct Fixture {
  ObjectStoreOptions options;
  ChatPlanConfig config;
  Fixture() {
    const auto* out = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const auto base = fs::absolute(out ? out : ".q4t-work/batch-preparation");
    fs::create_directories(base);
    auto path = (base / "batch-preparation-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("private Batch fixture");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
    options.max_client_bytes = 256ULL << 20;
    options.max_total_bytes = 512ULL << 20;
    config.model = "cpu-preparation";
    config.model_aliases = {"cpu-alias"};
    config.structured = true;
    config.allow_media = true;
    auto files = std::make_shared<FileExpansionOptions>();
    files->extraction.legacy.work_directory = path + "/codec";
    config.file_inputs = files;
  }
};
BinaryAccess Access(std::string scope = "client:batch-preparation") {
  return {std::move(scope), Clock::now() + 30s, {}};
}
StateAccess State() {
  auto access = Access();
  return {access.scope, access.deadline, access.cancelled};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s %s: %s\n", reply.code.c_str(),
                 reply.value.GetString("param").c_str(), reply.message.c_str());
  return reply.ok();
}
ObjectRequest Request(ObjectKind kind, std::string id = {},
                      ObjectOperation op = ObjectOperation::kGet) {
  ObjectRequest request;
  request.scope = Access().scope;
  request.kind = kind;
  request.id = std::move(id);
  request.operation = op;
  request.ttl_seconds = 0;
  return request;
}
ObjectReply Run(ObjectStore& objects, const ObjectRequest& request) {
  return objects.Execute(request, Access().deadline);
}
std::string Upload(ObjectStore& objects, std::string_view content,
                   std::string_view name, std::string purpose = "user_data") {
  FileUpload upload(objects, Access(), "multipart/form-data; boundary=private");
  const std::string header =
      "--private\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\n" +
      purpose +
      "\r\n--private\r\nContent-Disposition: form-data; name=\"file\"; "
      "filename=\"" + std::string(name) + "\"\r\n\r\n";
  if (!Ok(upload.Feed(header, Access().deadline)) ||
      !Ok(upload.Feed(content, Access().deadline)) ||
      !Ok(upload.Feed("\r\n--private--\r\n", Access().deadline)))
    throw std::runtime_error("upload fixture");
  auto result = upload.Finish();
  if (!Ok(result)) throw std::runtime_error("upload commit fixture");
  return result.value.GetString("id");
}
Json Response(std::string input = "report the fact") {
  return Object({{"input", String(std::move(input))},
                  {"reasoning", Object({{"effort", String("none")}})}});
}
Json WithFile(const std::string& id, bool chat = false) {
  auto part = chat ? Object({{"type", String("file")},
                             {"file", Object({{"file_id", String(id)}})}})
                   : Object({{"type", String("input_file")},
                             {"file_id", String(id)}});
  auto message = Object({{"role", String("user")},
                          {"content", Array({part})}});
  return chat ? Object({{"messages", Array({message})}})
              : Object({{"input", Array({message})},
                         {"reasoning", Object({{"effort", String("none")}})}});
}
struct Work {
  std::string id, file, endpoint;
  BatchInputRow row;
};
Work Create(ObjectStore& objects, Json body,
             std::string endpoint = "/v1/responses") {
  Work work;
  work.endpoint = std::move(endpoint);
  work.row.line = 1;
  work.row.custom_id = "原始\n\"编号";
  work.row.body = std::move(body);
  const auto text = OutputJson(Object(
      {{"custom_id", String(work.row.custom_id)}, {"method", String("POST")},
       {"url", String(work.endpoint)}, {"body", work.row.body}})) + '\n';
  work.row.wire_bytes = text.size();
  work.file = Upload(objects, text, "requests.jsonl", "batch");
  auto result = BatchState(objects).Create(
      Access(), {work.file, work.endpoint, Object()});
  if (!Ok(result)) throw std::runtime_error("Batch root fixture");
  work.id = result.value.GetString("id");
  return work;
}
struct ValidationWork {
  std::string id, file, raw;
  std::vector<BatchRowPosition> positions;
};
ValidationWork ValidationInput(ObjectStore& objects,
                               const std::vector<Json>& bodies,
                               bool duplicate = false,
                               bool final_newline = true) {
  ValidationWork work;
  for (size_t i = 0; i < bodies.size(); ++i) {
    const auto id = "validation-" + std::to_string(duplicate ? 0 : i);
    const auto line = OutputJson(Object(
        {{"custom_id", String(id)}, {"method", String("POST")},
         {"url", String("/v1/responses")}, {"body", bodies[i]}})) +
        (i + 1 == bodies.size() && !final_newline ? "" : "\r\n");
    work.positions.push_back({i + 1, work.raw.size(), line.size(), Digest(id)});
    work.raw += line;
  }
  work.file = Upload(objects, work.raw, "validation.jsonl", "batch");
  const auto result = BatchState(objects).Create(
      Access(), {work.file, "/v1/responses", Object()});
  if (!Ok(result)) throw std::runtime_error("validation input fixture");
  work.id = result.value.GetString("id");
  return work;
}
bool Publish(ObjectStore& objects, const Work& work,
               BatchPreparedInput* prepared) {
  BatchRowPosition position{work.row.line,          work.row.offset,
                            work.row.wire_bytes,    Digest(work.row.custom_id),
                            prepared->reference,    prepared->binding_sha256,
                            prepared->inline_input, prepared->input_budget};
  auto result =
      prepared->input_budget.tokens
          ? BatchState(objects).AppendValidatedRow(Access(), work.id, position,
                                                   prepared->canonical_model)
          : BatchState(objects).AppendValidationPage(Access(), work.id,
                                                     std::span(&position, 1));
  BatchPreparation(objects).Published(Access(), prepared, result.commit_unknown);
  return Ok(result);
}
ObjectReply Load(ObjectStore& objects, const Work& work, Json* data) {
  auto checkpoint = BatchState(objects).Row(Access(), work.id, 0);
  if (!checkpoint.ok()) return checkpoint;
  return BatchPreparation(objects).Read(Access(), work.id, work.endpoint,
                                          work.row, checkpoint.value, data);
}
std::string Prompt(const ResponsesExecution& run) {
  std::string prompt, param;
  std::vector<VisionItem> images;
  const auto& generation = run.Generation();
  auto status = PrepareChatPrompt(
      generation.chat.Request(generation.generation_request),
      generation.chat.constraints, &prompt, &images, &param);
  if (!status) throw std::runtime_error(status.message());
  return prompt;
}
ObjectReply FinishFixture(ResponsesExecution& run) {
  std::string wire;
  auto reply = run.Begin(&wire);
  if (!reply.ok()) return reply;
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = "CPU lifecycle fixture; no model invocation";
  if (!run.Append(delta, "null", &wire))
    throw std::runtime_error("fixture append");
  return run.Finish(false, 0, {8, 0, 4, 0}, std::time(nullptr), &wire);
}
}  // namespace

Q4T_TEST(batch_preparation_deleted_files_and_previous_restore_after_restart) {
  Fixture fixture;
  Work work;
  std::string id;
  {
    ObjectStore objects(fixture.options);
    const auto file = Upload(objects, "preserved fact: violet station 42",
                               "facts.txt");
    ResponsesExecution previous(objects);
    Q4T_CHECK(Ok(previous.Prepare(WithFile(file), fixture.config, State(), 42)));
    Q4T_CHECK(Ok(FinishFixture(previous)));
    auto request = Response();
    Set(&request, "previous_response_id", String(previous.Id()));
    Set(&request, "store", Boolean(false));
    work = Create(objects, request);
    BatchPreparedInput prepared;
    Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(
        Access(), work.id, work.endpoint, work.row, fixture.config, &prepared)));
    Q4T_CHECK(Publish(objects, work, &prepared));
    Json data;
    Q4T_CHECK(Ok(Load(objects, work, &data)));
    id = data.GetString("id");
    Q4T_CHECK(ResponsesState(objects).Retrieve(id, State()).http_status == 404);
    Q4T_CHECK(Ok(ResponsesState(objects).Erase(previous.Id(), State())));
    Q4T_CHECK(Ok(Files(objects).Erase(Access(), file)));
    Q4T_CHECK(Ok(Files(objects).Erase(Access(), work.file)));
  }
  detail::SetDocumentWorkerEntryForTesting("--grammar-fixture-silent");
  ScopeExit reset([] { detail::SetDocumentWorkerEntryForTesting({}); });
  ObjectStore objects(fixture.options);
  Json data;
  Q4T_CHECK(Ok(Load(objects, work, &data)));
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.RestoreInputs(data, fixture.config, State(), 44)));
  Q4T_CHECK(run.Id() == id && !run.Begun() && run.GenerationReady());
  Q4T_CHECK(Prompt(run).find("preserved fact: violet station 42") !=
            std::string::npos);
  Q4T_CHECK(Ok(FinishFixture(run)));
  Q4T_CHECK(ResponsesState(objects).Retrieve(id, State()).http_status == 404);
  return true;
}

Q4T_TEST(batch_preparation_conversation_change_keeps_input_and_conflicts_commit) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  auto original = ResponsesState(objects).ResolveItems(
      Array({Object({{"role", String("user")},
                      {"content", String("original context 42")}})}), State());
  Q4T_CHECK(Ok(original));
  auto created = ConversationState(objects).Create(
      Object(), original.value, State());
  Q4T_CHECK(Ok(created));
  auto request = Response("use the original context");
  const auto id = created.value.GetString("id");
  Set(&request, "conversation", String(id));
  auto work = Create(objects, request);
  BatchPreparedInput prepared;
  Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(
      Access(), work.id, work.endpoint, work.row, fixture.config, &prepared)));
  Q4T_CHECK(Publish(objects, work, &prepared));
  auto later = ResponsesState(objects).ResolveItems(
      Array({Object({{"role", String("user")},
                      {"content", String("later conflicting context 99")}})}),
      State());
  Q4T_CHECK(Ok(later) &&
            Ok(ConversationState(objects).Append(id, later.value, {}, State())));
  Json data;
  Q4T_CHECK(Ok(Load(objects, work, &data)));
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.RestoreInputs(data, fixture.config, State(), 45)));
  const auto prompt = Prompt(run);
  Q4T_CHECK(prompt.find("original context 42") != prompt.npos &&
            prompt.find("later conflicting context 99") == prompt.npos);
  auto result = FinishFixture(run);
  Q4T_CHECK(result.http_status == 409 && !run.Finished());
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  const auto items = ConversationState(objects).ListItems(id, {}, State());
  Q4T_CHECK(Ok(items) && items.value.GetArray("data")->array.size() == 2);
  return true;
}

Q4T_TEST(batch_preparation_csv_store_false_restores_unexecuted_sheet) {
  Fixture fixture;
  Work work;
  {
    ObjectStore objects(fixture.options);
    const auto file = Upload(objects, "name,value\nalpha,42\nbeta,7\n",
                               "facts.csv");
    auto request = WithFile(file);
    Set(&request, "store", Boolean(false));
    work = Create(objects, request);
    BatchPreparedInput prepared;
    Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(
        Access(), work.id, work.endpoint, work.row, fixture.config, &prepared)));
    Q4T_CHECK(Publish(objects, work, &prepared));
    Q4T_CHECK(Ok(Files(objects).Erase(Access(), file)));
    Q4T_CHECK(Run(objects, Request(ObjectKind::kResponse, {},
                                   ObjectOperation::kList))
                  .value.GetArray("data")->array.empty());
  }
  ObjectStore objects(fixture.options);
  Json data;
  Q4T_CHECK(Ok(Load(objects, work, &data)));
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.RestoreInputs(data, fixture.config, State(), 45)));
  Q4T_CHECK(run.PendingFileSheets() && !run.GenerationReady() && !run.Begun());
  FileSheetStep sheet;
  Q4T_CHECK(Ok(run.NextFileSheet(fixture.config, &sheet)));
  Q4T_CHECK(OutputJson(sheet.request).find("alpha") != std::string::npos &&
            OutputJson(sheet.request).find("42") != std::string::npos);
  Q4T_CHECK(run.FileUsage()->model_steps == 0 &&
            run.FileUsage()->model_input_tokens == 0);
  return true;
}

Q4T_TEST(batch_preparation_chat_pdf_restores_original_bytes_and_store_binding) {
  Fixture fixture;
  Work work;
  Json before;
  std::string file;
  {
    ObjectStore objects(fixture.options);
    std::ifstream input(fs::path(__FILE__).parent_path() /
                            "fixtures/file_nested/page.pdf",
                        std::ios::binary);
    Q4T_CHECK(input.good());
    const std::string bytes(std::istreambuf_iterator<char>(input), {});
    file = Upload(objects, bytes, "original.pdf");
    auto request = WithFile(file, true);
    Set(&request, "store", Boolean(true));
    ChatRequestPlan plan;
    ChatFileInputs files(&objects);
    Q4T_CHECK(Ok(files.Prepare(request, fixture.config, Access(), &plan)));
    before = plan.Request(request);
    work = Create(objects, request, "/v1/chat/completions");
    BatchPreparedInput prepared;
    Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(
        Access(), work.id, work.endpoint, work.row, fixture.config, &prepared)));
    Q4T_CHECK(Publish(objects, work, &prepared));
    Q4T_CHECK(Ok(Files(objects).Erase(Access(), file)));
  }
  ObjectStore objects(fixture.options);
  Json data;
  Q4T_CHECK(Ok(Load(objects, work, &data)));
  ChatFileInputs files(&objects);
  ChatRequestPlan plan;
  Q4T_CHECK(Ok(files.Prepare(work.row.body, fixture.config, Access(), &plan,
                             *data.Find("input_snapshots"), true)));
  Q4T_CHECK(OutputJson(plan.Request(work.row.body)) == OutputJson(before));
  Json stored;
  const auto id = NewObjectId(ObjectKind::kChat);
  Q4T_CHECK(Ok(files.Persist(id, Access(), &stored)));
  Q4T_CHECK(stored.array.size() == 1 &&
            stored.array[0].GetString("item_id") == StoredChatMessageId(id, 0));
  Q4T_CHECK(OutputJson(work.row.body).find(file) != std::string::npos);
  return true;
}

Q4T_TEST(batch_preparation_original_row_and_scope_are_bound_transactionally) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  auto work = Create(objects, Response());
  BatchPreparedInput prepared;
  BatchPreparation preparation(objects);
  Q4T_CHECK(Ok(preparation.Prepare(Access(), work.id, work.endpoint, work.row,
                                   fixture.config, &prepared)));
  Q4T_CHECK(Publish(objects, work, &prepared));
  const auto row = BatchState(objects).Row(Access(), work.id, 0);
  Q4T_CHECK(Ok(row));
  for (int i = 0; i < 5; ++i) {
    auto changed = work;
    auto access = Access();
    if (i == 0) changed.row.custom_id += "changed";
    if (i == 1) Set(&changed.row.body, "input", String("different input"));
    if (i == 2) changed.row.line = 2;
    if (i == 3) changed.id = NewObjectId(ObjectKind::kBatch);
    if (i == 4) access.scope = "client:another";
    Json output = String("untouched");
    const auto reply = preparation.Read(access, changed.id, changed.endpoint,
                                         changed.row, row.value, &output);
    Q4T_CHECK(!reply.ok() && output.str == "untouched");
  }
  auto reordered = work.row;
  std::reverse(reordered.body.object.begin(), reordered.body.object.end());
  Json restored;
  Q4T_CHECK(Ok(preparation.Read(Access(), work.id, work.endpoint, reordered,
                                 row.value, &restored)));
  return true;
}

Q4T_TEST(batch_preparation_native_planners_reject_before_publication) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto id = NewObjectId(ObjectKind::kBatch);
  for (int i = 0; i < 4; ++i) {
    BatchInputRow row;
    row.line = 1;
    row.custom_id = "bad";
    row.body = i == 0 ? Object({{"prompt", Number(42)}})
                     : i == 1 ? Object({{"messages", String("bad")}})
                              : Response();
    const std::string endpoint = i == 0 ? "/v1/completions"
                                : i == 1 ? "/v1/chat/completions"
                                         : "/v1/responses";
    if (i == 2) Set(&row.body, "stream", Boolean(true));
    if (i == 3) Set(&row.body, "tools", Array({Object()}));
    BatchPreparedInput prepared;
    auto reply = BatchPreparation(objects).Prepare(
        Access(), id, endpoint, row, fixture.config, &prepared);
    Q4T_CHECK(reply.http_status == 400 && prepared.reference.id.empty());
  }
  Q4T_CHECK(objects.Health().workers_started == 0);
  return true;
}

Q4T_TEST(batch_preparation_completion_alias_and_raw_tokens_use_native_contract) {
  Fixture fixture;
  fixture.config.token_vocab = 256;
  ObjectStore objects(fixture.options);
  auto work = Create(objects,
                     Object({{"model", String("cpu-alias")},
                             {"prompt", Array({Number(10), Number(11)})},
                             {"echo", Boolean(true)},
                             {"max_tokens", Number(0)}}),
                     "/v1/completions");
  BatchPreparedInput prepared;
  Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(
      Access(), work.id, work.endpoint, work.row, fixture.config, &prepared)));
  Q4T_CHECK(prepared.canonical_model == fixture.config.model);
  Q4T_CHECK(Publish(objects, work, &prepared));
  Json data;
  Q4T_CHECK(Ok(Load(objects, work, &data)) &&
            data.GetString("kind") == "completion");
  BatchInputRow original;
  Q4T_CHECK(Ok(BatchState(objects).ReadInputRow(Access(), work.id, 0,
                                                &original)));
  Q4T_CHECK(OutputJson(original.body) == OutputJson(work.row.body));
  return true;
}

Q4T_TEST(batch_preparation_response_policy_scope_and_frozen_files_are_enforced) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto file = Upload(objects, "file fact", "fact.txt");
  auto work = Create(objects, WithFile(file));
  BatchPreparedInput prepared;
  Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(
      Access(), work.id, work.endpoint, work.row, fixture.config, &prepared)));
  Q4T_CHECK(Publish(objects, work, &prepared));
  Json data;
  Q4T_CHECK(Ok(Load(objects, work, &data)));
  for (int i = 0; i < 5; ++i) {
    auto altered = data;
    auto config = fixture.config;
    auto access = State();
    if (i == 0) access.scope = "client:foreign";
    if (i == 1) Set(&altered, "input_snapshots", Array());
    if (i == 2) Set(&Field(altered, "options"), "max_output_tokens", Number(77));
    if (i == 3) Set(&altered, "store", Number(1));
    if (i == 4) access.cancelled = [] { return true; };
    ResponsesExecution run(objects);
    auto result = run.RestoreInputs(altered, config, access, 46);
    Q4T_CHECK(!result.ok() && !run.Begun());
    Q4T_CHECK(ResponsesState(objects).Retrieve(data.GetString("id"), State())
                  .http_status == 404);
  }
  return true;
}

Q4T_TEST(batch_preparation_large_history_uses_private_chunks_not_metadata_limit) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto text = std::string((17ULL << 20) + 31, 'x') + " exact tail 42";
  auto work = Create(objects, Response(text));
  BatchPreparedInput prepared;
  Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(
      Access(), work.id, work.endpoint, work.row, fixture.config, &prepared)));
  Q4T_CHECK(Publish(objects, work, &prepared));
  Q4T_CHECK(Ok(Files(objects).Erase(Access(), work.file)));
  Json data;
  Q4T_CHECK(Ok(Load(objects, work, &data)));
  Q4T_CHECK(data.Find("request")->GetString("input") == text);
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.RestoreInputs(data, fixture.config, State(), 47)));
  Q4T_CHECK(Prompt(run).find(text) != std::string::npos);
  return true;
}

Q4T_TEST(batch_preparation_atomic_page_rejects_changed_snapshot_identity) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  auto work =
      Create(objects, Response(std::string(kBatchInlineBytes + 1, 's')));
  BatchPreparedInput prepared;
  Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(
      Access(), work.id, work.endpoint, work.row, fixture.config, &prepared)));
  BatchRowPosition position{1, 0, work.row.wire_bytes,
                             Digest(work.row.custom_id), prepared.reference,
                             prepared.binding_sha256};
  position.prepared_input.sha256 = Digest("wrong root");
  auto reply = BatchState(objects).AppendValidationPage(
      Access(), work.id, std::span(&position, 1));
  Q4T_CHECK(reply.code == "input_snapshot_reference_conflict" &&
            !reply.commit_unknown);
  const auto root = BatchState(objects).Load(Access(), work.id);
  Q4T_CHECK(Ok(root) && root.value.Find("data")->GetInt("validated_rows") == 0);
  Q4T_CHECK(Publish(objects, work, &prepared));
  return true;
}

Q4T_TEST(batch_preparation_unknown_snapshot_receipt_keeps_private_identity) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 100;
  Work work;
  {
    ObjectStore objects(fixture.options);
    work = Create(objects, Response(std::string(kBatchInlineBytes + 1, 's')));
  }
  // Restart only this fixture worker with the real commit/receipt-loss hook.
  ObjectWorkerEntryForTesting("--q4t-object-worker-snapshot-lost-ack");
  ScopeExit reset([] { ObjectWorkerEntryForTesting({}); });
  ObjectStore objects(fixture.options);
  BatchPreparedInput prepared;
  auto reply = BatchPreparation(objects).Prepare(
      Access(), work.id, work.endpoint, work.row, fixture.config, &prepared);
  Q4T_CHECK(!reply.ok() && reply.commit_unknown &&
            !prepared.reference.id.empty() && prepared.hold.commit_unknown);
  BatchPreparation(objects).Published(Access(), &prepared, true);
  Q4T_CHECK(!prepared.hold.released);
  ObjectWorkerEntryForTesting({});
  const auto deadline = Clock::now() + 1s;
  while (objects.Health().pending && Clock::now() < deadline)
    std::this_thread::sleep_for(1ms);
  const auto root = Run(objects, Request(ObjectKind::kInputSnapshot,
                                         prepared.reference.id));
  Q4T_CHECK(Ok(root) &&
            Digest(OutputJson(*root.value.Find("data"))) ==
                prepared.reference.sha256);
  Q4T_CHECK(Run(objects, Request(ObjectKind::kResponse, {},
                                 ObjectOperation::kList))
                .value.GetArray("data")->array.empty());
  return true;
}

Q4T_TEST(batch_preparation_snapshot_dependencies_checked_before_atomic_publish) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  InputSnapshotRef child;
  InputSnapshotHold hold;
  const auto binding = Digest("child identity");
  const std::string_view part = "actual child bytes";
  Q4T_CHECK(Ok(snapshots.Save(Access(), binding, Object(), std::span(&part, 1),
                              {}, 60, &child, &hold)));
  for (int i = 0; i < 3; ++i) {
    auto reference = child;
    auto child_binding = binding;
    auto access = Access();
    if (i == 0) reference.sha256 = Digest("changed identity");
    if (i == 1) child_binding = Digest("changed binding");
    if (i == 2) access.scope = "client:foreign";
    InputSnapshotRef parent;
    InputSnapshotHold parent_hold;
    const auto dependencies = Array({Object(
        {{"snapshot", InputSnapshotReference(reference)},
         {"binding_sha256", String(child_binding)}})});
    auto reply = snapshots.Save(access, Digest("parent"), Object(),
                                  std::span(&part, 1), {}, 60, &parent,
                                  &parent_hold, dependencies);
    Q4T_CHECK(reply.code == "input_snapshot_reference_conflict" &&
              !reply.commit_unknown);
    Q4T_CHECK(parent.id.empty() && parent_hold.id.empty());
    auto request = Request(ObjectKind::kInputSnapshot, {},
                             ObjectOperation::kList);
    request.scope = access.scope;
    const auto listed = Run(objects, request);
    Q4T_CHECK(Ok(listed) && listed.value.GetArray("data")->array.size() ==
                               (i == 2 ? 0U : 1U));
  }
  InputSnapshot result;
  Q4T_CHECK(Ok(snapshots.Read(Access(), child, binding, {}, &result)) &&
            result.parts == std::vector<std::string>{std::string(part)});
  return true;
}

Q4T_TEST(batch_validation_driver_yields_one_row_and_admits_only_after_whole_file) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto work = ValidationInput(
      objects, {Response("first"), Response("second"), Response("third")});
  BatchValidation driver(objects, work.id, fixture.config);
  for (size_t i = 0; i < 3; ++i) {
    Q4T_CHECK(Ok(driver.Step(Access())) && driver.RowsChecked() == i + 1);
    const auto loaded = BatchState(objects).Load(Access(), work.id);
    Q4T_CHECK(Ok(loaded));
    const auto& data = *loaded.value.Find("data");
    const auto& batch = *data.Find("batch");
    Q4T_CHECK(data.GetInt("validated_rows") == static_cast<int64_t>(i + 1) &&
              batch.GetString("status") ==
                  (i < 2 ? "validating" : "in_progress") &&
              batch.Find("request_counts")->GetInt("total") == (i < 2 ? 0 : 3));
    if (i < 2)
      Q4T_CHECK(BatchState(objects).StartRow(Access(), work.id, 0).http_status ==
                409);
  }
  Q4T_CHECK(driver.Complete() && driver.BytesChecked() == work.raw.size());
  Q4T_CHECK(Run(objects, Request(ObjectKind::kResponse, {},
                                 ObjectOperation::kList))
                .value.GetArray("data")->array.empty());
  return true;
}

Q4T_TEST(batch_validation_driver_restart_rechecks_source_and_reuses_deleted_file) {
  Fixture fixture;
  ValidationWork work;
  Json original;
  {
    ObjectStore objects(fixture.options);
    const auto file = Upload(objects, "permanent queued fact 42", "fact.txt");
    work = ValidationInput(objects, {WithFile(file), Response("later")});
    BatchValidation driver(objects, work.id, fixture.config);
    Q4T_CHECK(Ok(driver.Step(Access())) && driver.RowsChecked() == 1 &&
              !driver.Complete());
    original = BatchState(objects).Row(Access(), work.id, 0).value;
    Q4T_CHECK(Ok(Files(objects).Erase(Access(), file)) &&
              Ok(Files(objects).Erase(Access(), work.file)));
  }
  ObjectStore objects(fixture.options);
  BatchValidation restored(objects, work.id, fixture.config);
  Q4T_CHECK(Ok(restored.Step(Access())) && restored.RowsChecked() == 1);
  Q4T_CHECK(OutputJson(BatchState(objects).Row(Access(), work.id, 0).value) ==
            OutputJson(original));
  Q4T_CHECK(Ok(restored.Step(Access())) && restored.Complete() &&
            restored.RowsChecked() == 2);
  BatchInputRow row;
  Q4T_CHECK(Ok(BatchState(objects).ReadInputRow(Access(), work.id, 0, &row)));
  Json frozen;
  Q4T_CHECK(Ok(BatchPreparation(objects).Read(
      Access(), work.id, "/v1/responses", row, original, &frozen)));
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.RestoreInputs(frozen, fixture.config, State(), 43)) &&
            Prompt(run).find("permanent queued fact 42") != std::string::npos);
  return true;
}

Q4T_TEST(batch_validation_driver_duplicate_after_recovery_never_prepares_next_row) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto work = ValidationInput(
      objects, {Response("first"), WithFile("file_not_present")}, true);
  {
    BatchValidation driver(objects, work.id, fixture.config);
    Q4T_CHECK(Ok(driver.Step(Access())) && driver.RowsChecked() == 1);
  }
  BatchValidation restored(objects, work.id, fixture.config);
  Q4T_CHECK(Ok(restored.Step(Access())));
  const auto failed = restored.Step(Access());
  Q4T_CHECK(failed.http_status == 400 &&
            failed.value.GetString("param") == "custom_id" &&
            failed.value.GetInt("line") == 2 && !restored.Complete());
  const auto before = BatchState(objects).Load(Access(), work.id);
  Q4T_CHECK(Ok(before) && before.value.Find("data")->GetInt("validated_rows") ==
                               1);
  Q4T_CHECK(restored.Step(Access()).message == failed.message &&
            OutputJson(BatchState(objects).Load(Access(), work.id).value) ==
                OutputJson(before.value));
  return true;
}

Q4T_TEST(batch_validation_driver_late_native_error_has_original_line_and_no_execution) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto invalid = Object({{"input", String("bad")},
                                {"tools", Array({Object()})}});
  const auto work = ValidationInput(objects, {Response(), invalid});
  BatchValidation driver(objects, work.id, fixture.config);
  Q4T_CHECK(Ok(driver.Step(Access())));
  const auto failed = driver.Step(Access());
  Q4T_CHECK(failed.http_status == 400 && failed.value.GetInt("line") == 2 &&
            !failed.value.GetString("param").empty() && !driver.Complete());
  BatchState batches(objects);
  Q4T_CHECK(batches.StartRow(Access(), work.id, 0).http_status == 409);
  Q4T_CHECK(Ok(batches.FailValidation(Access(), work.id, failed)));
  const auto terminal = batches.Retrieve(Access(), work.id);
  Q4T_CHECK(Ok(terminal) && terminal.value.GetString("status") == "failed" &&
            terminal.value.Find("errors")->GetArray("data")->array[0]
                .GetInt("line") == 2 &&
            terminal.value.Find("request_counts")->GetInt("completed") == 0);
  return true;
}

Q4T_TEST(batch_validation_driver_model_change_cannot_reinterpret_frozen_prefix) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto work = ValidationInput(objects, {Response(), Response("next")});
  {
    BatchValidation driver(objects, work.id, fixture.config);
    Q4T_CHECK(Ok(driver.Step(Access())));
  }
  auto config = fixture.config;
  config.model = "different-served-model";
  BatchValidation restored(objects, work.id, config);
  Q4T_CHECK(Ok(restored.Step(Access())) && restored.RowsChecked() == 1);
  const auto failed = restored.Step(Access());
  Q4T_CHECK(failed.http_status == 400 &&
            failed.value.GetString("param") == "body.model" &&
            failed.value.GetInt("line") == 2);
  const auto current = BatchState(objects).Load(Access(), work.id);
  Q4T_CHECK(Ok(current) &&
            current.value.Find("data")->GetString("canonical_model") ==
                fixture.config.model &&
            current.value.Find("data")->GetInt("validated_rows") == 1);
  return true;
}

Q4T_TEST(batch_validation_driver_cancel_scope_and_completed_scope_are_bound) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto work = ValidationInput(objects, {Response(), Response("next")});
  BatchValidation driver(objects, work.id, fixture.config);
  Q4T_CHECK(Ok(driver.Step(Access())));
  auto foreign = Access("client:foreign");
  Q4T_CHECK(!driver.Step(foreign).ok() && driver.RowsChecked() == 1);
  Q4T_CHECK(Ok(driver.Step(Access())) && driver.Complete());
  const auto leaked = driver.Step(foreign);
  Q4T_CHECK(!leaked.ok() && leaked.value.GetString("id").empty());
  const auto other = ValidationInput(objects, {Response(), Response("next")});
  BatchValidation cancelled(objects, other.id, fixture.config);
  Q4T_CHECK(Ok(cancelled.Step(Access())));
  Q4T_CHECK(Ok(BatchState(objects).Cancel(Access(), other.id)));
  Q4T_CHECK(cancelled.Step(Access()).code == "batch_cancelled" &&
            cancelled.RowsChecked() == 1 && !cancelled.Complete());
  return true;
}

Q4T_TEST(batch_validation_driver_chunk_crossing_final_line_and_short_pins) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto text = std::string(kBinaryChunkBytes + 31, 'q') + " tail 42";
  const auto work = ValidationInput(objects, {Response(text), Response()}, false,
                                      false);
  BatchValidation driver(objects, work.id, fixture.config);
  Q4T_CHECK(Ok(driver.Step(Access())) && driver.RowsChecked() == 1);
  auto request = Request(ObjectKind::kToolRun, {}, ObjectOperation::kList);
  request.limit = 100;
  const auto private_objects = Run(objects, request);
  Q4T_CHECK(Ok(private_objects));
  for (const auto& record : private_objects.value.GetArray("data")->array)
    Q4T_CHECK(record.Find("data")->GetString("format") != "q4t.binary_read.v1");
  Q4T_CHECK(Ok(driver.Step(Access())) && driver.Complete() &&
            driver.BytesChecked() == work.raw.size());
  BatchInputRow original;
  Q4T_CHECK(Ok(BatchState(objects).ReadInputRow(Access(), work.id, 0, &original)));
  Q4T_CHECK(original.body.GetString("input") == text);
  return true;
}

Q4T_TEST(batch_validation_driver_unknown_source_pin_is_sticky_and_resumable) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 100;
  ValidationWork work;
  {
    ObjectStore objects(fixture.options);
    work = ValidationInput(objects, {Response()});
  }
  ObjectWorkerEntryForTesting("--q4t-object-worker-application-lost-ack");
  ScopeExit reset([] { ObjectWorkerEntryForTesting({}); });
  {
    ObjectStore objects(fixture.options);
    BatchValidation failed(objects, work.id, fixture.config);
    const auto result = failed.Step(Access());
    Q4T_CHECK(result.commit_unknown && failed.RowsChecked() == 0 &&
              !failed.Complete() && failed.Step(Access()).commit_unknown);
  }
  // The worker entry is captured when ObjectStore is constructed. Recreate
  // the controller after clearing the injected entry, preserving the actual
  // committed read pin and original durable Batch on disk.
  ObjectWorkerEntryForTesting({});
  ObjectStore objects(fixture.options);
  BatchValidation restored(objects, work.id, fixture.config);
  Q4T_CHECK(Ok(restored.Step(Access())) && restored.Complete() &&
            restored.RowsChecked() == 1);
  return true;
}

Q4T_TEST(batch_validation_driver_unfrozen_legacy_positions_are_revalidated) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto work = ValidationInput(objects, {Response(), Response("next")});
  Q4T_CHECK(Ok(BatchState(objects).AppendValidationPage(
      Access(), work.id, std::span(&work.positions[0], 1))));
  BatchValidation driver(objects, work.id, fixture.config);
  Q4T_CHECK(Ok(driver.Step(Access())) && driver.RowsChecked() == 1 &&
            !driver.Complete());
  const auto restored = BatchState(objects).Load(Access(), work.id);
  Q4T_CHECK(Ok(restored) && restored.value.Find("data")->GetBool("inputs_frozen") &&
            restored.value.Find("data")->GetString("canonical_model") ==
                fixture.config.model);
  Q4T_CHECK(Ok(driver.Step(Access())) && driver.Complete());
  return true;
}

Q4T_TEST(batch_inline_input_exact_byte_boundary_and_durable_fallback) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto body = [](size_t input, size_t metadata) {
    auto request = Response(std::string(input, 'x'));
    Set(&request, "metadata",
        Object({{"padding", String(std::string(metadata, 'm'))}}));
    return request;
  };
  const auto measure = [&](size_t input, size_t metadata) {
    auto sample = Create(objects, body(input, metadata));
    BatchPreparedInput value;
    if (!Ok(BatchPreparation(objects).Prepare(Access(), sample.id,
                                              sample.endpoint, sample.row,
                                              fixture.config, &value)) ||
        !ValidBatchInlineInput(value.inline_input))
      throw std::runtime_error("bounded inline boundary fixture");
    return value.inline_input.GetString("payload").size();
  };
  // The frozen request/history/own_input retain several copies. Measure
  // small valid payloads to construct an exact byte boundary independently
  // of that representation, without weakening the 8192/8193 assertions.
  const auto base_bytes = measure(1, 1);
  const auto input_step = measure(2, 1) - base_bytes;
  const auto metadata_step = measure(1, 2) - base_bytes;
  Q4T_CHECK(base_bytes > 0 && base_bytes < kBatchInlineBytes);
  Q4T_CHECK(input_step > 0 && metadata_step > 0);
  for (size_t extra : {size_t{0}, size_t{1}}) {
    const auto available = kBatchInlineBytes + extra - base_bytes;
    size_t padding = 0;
    while (padding < input_step &&
           (available - padding * metadata_step) % input_step)
      ++padding;
    Q4T_CHECK(padding < input_step && padding * metadata_step < available);
    auto work = Create(
        objects, body(1 + (available - padding * metadata_step) / input_step,
                      1 + padding));
    BatchPreparedInput prepared;
    Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(Access(), work.id,
                                                   work.endpoint, work.row,
                                                   fixture.config, &prepared)));
    if (extra == 0) {
      Q4T_CHECK(ValidBatchInlineInput(prepared.inline_input) &&
                prepared.inline_input.GetString("payload").size() ==
                    kBatchInlineBytes &&
                prepared.reference.id.empty() && prepared.hold.id.empty());
    } else {
      Q4T_CHECK(prepared.inline_input.IsNull() &&
                !prepared.reference.id.empty() && !prepared.hold.id.empty());
      InputSnapshot saved;
      Q4T_CHECK(Ok(InputSnapshotStore(objects).Read(
          Access(), prepared.reference, prepared.binding_sha256,
          {kBackgroundInputBytes, 16384, 1}, &saved)));
      Q4T_CHECK(saved.parts.size() == 1 &&
                saved.parts[0].size() == kBatchInlineBytes + 1);
    }
    Q4T_CHECK(Publish(objects, work, &prepared));
    Json restored;
    Q4T_CHECK(Ok(Load(objects, work, &restored)) &&
              restored.Find("request")->GetString("input") ==
                  work.row.body.GetString("input"));
  }
  return true;
}

Q4T_TEST(batch_inline_input_scope_deadline_binding_and_immutable_page) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto work = Create(objects, Response("immutable input 42"));
  BatchPreparedInput prepared;
  Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(
      Access(), work.id, work.endpoint, work.row, fixture.config, &prepared)));
  Q4T_CHECK(ValidBatchInlineInput(prepared.inline_input) &&
            Publish(objects, work, &prepared));
  auto checkpoint = BatchState(objects).Row(Access(), work.id, 0);
  Q4T_CHECK(Ok(checkpoint));
  for (int which = 0; which < 6; ++which) {
    auto access = Access();
    auto row = work.row;
    auto stored = checkpoint.value;
    Json output = String("unchanged");
    if (which == 0) access.scope = "client:other";
    if (which == 1) access.deadline = Clock::now() - 1ms;
    if (which == 2) access.cancelled = [] { return true; };
    if (which == 3) Set(&row.body, "input", String("changed request"));
    if (which == 4)
      Set(&Field(stored, "prepared_inline"), "payload", String("{}"));
    if (which == 5) Set(&stored, "input_snapshots", Array());
    const auto reply = BatchPreparation(objects).Read(
        access, work.id, work.endpoint, row, stored, &output);
    Q4T_CHECK(!reply.ok() && output.IsString() && output.str == "unchanged");
  }
  auto loaded = BatchState(objects).Load(Access(), work.id);
  const auto page_id =
      loaded.value.Find("data")->GetArray("pages")->array[0].str;
  auto request = Request(ObjectKind::kToolRun, page_id);
  auto page = Run(objects, request);
  Q4T_CHECK(Ok(page));
  request.operation = ObjectOperation::kPut;
  request.expected_revision = page.value.GetInt("revision");
  request.created_at = page.value.GetInt("created_at");
  request.expires_at = page.value.GetInt("expires_at");
  request.data = *page.value.Find("data");
  for (int which = 0; which < 4; ++which) {
    auto changed = request;
    auto& row = Field(changed.data, "rows").array[0];
    if (which == 0) Set(&row, "custom_id_sha256", String(Digest("changed")));
    if (which == 1) {
      auto& value = Field(row, "prepared_inline");
      Set(&value, "payload", String("{\"kind\":\"completion\"}"));
      Set(&value, "sha256", String(Digest(value.GetString("payload"))));
    }
    if (which == 2) Set(&changed.data, "format", String("q4t.batch_page.v1"));
    if (which == 3) Set(&changed.data, "rows", Array());
    auto reply = which % 2 ? objects.ExecuteBatch(std::span(&changed, 1),
                                                  Access().deadline)
                           : Run(objects, changed);
    Q4T_CHECK(reply.code == "batch_checkpoint_immutable" &&
              !reply.commit_unknown);
  }
  Json restored;
  Q4T_CHECK(Ok(Load(objects, work, &restored)) &&
            restored.Find("request")->GetString("input") ==
                "immutable input 42");
  return true;
}

Q4T_TEST(batch_inline_input_actual_lost_page_receipt_survives_restart) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 150;
  Work work;
  BatchPreparedInput prepared;
  {
    ObjectStore objects(fixture.options);
    work = Create(objects, Response("frozen only once"));
    Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(Access(), work.id,
                                                   work.endpoint, work.row,
                                                   fixture.config, &prepared)));
    Q4T_CHECK(ValidBatchInlineInput(prepared.inline_input));
  }
  ObjectWorkerEntryForTesting("--q4t-object-worker-application-lost-ack");
  ScopeExit reset([] { ObjectWorkerEntryForTesting({}); });
  {
    ObjectStore objects(fixture.options);
    BatchRowPosition position{work.row.line,
                              work.row.offset,
                              work.row.wire_bytes,
                              Digest(work.row.custom_id),
                              {},
                              prepared.binding_sha256,
                              prepared.inline_input};
    auto result = BatchState(objects).AppendValidatedRow(
        Access(), work.id, position, prepared.canonical_model);
    Q4T_CHECK(!result.ok() && result.commit_unknown);
    BatchPreparation(objects).Published(Access(), &prepared, true);
  }
  ObjectWorkerEntryForTesting({});
  ObjectStore objects(fixture.options);
  auto root = BatchState(objects).Load(Access(), work.id);
  Q4T_CHECK(Ok(root) && root.value.Find("data")->GetInt("validated_rows") == 1);
  Json restored;
  Q4T_CHECK(Ok(Load(objects, work, &restored)) &&
            restored.Find("request")->GetString("input") == "frozen only once");
  return true;
}

namespace {
// Explicit CPU codec: one byte per text token, with real single-token FIM/
// seed and visual placeholder contracts. No claim of model BPE equivalence.
constexpr std::string_view kBudgetSpecials[] = {
    "<|fim_prefix|>", "<|fim_suffix|>", "<|fim_middle|>", "<|endoftext|>"};
BatchTokenCodec BudgetCodec(std::string* encoded = nullptr) {
  BatchTokenCodec codec;
  codec.encode = [encoded](std::string_view text, std::vector<uint32_t>* out) {
    if (encoded) *encoded = text;
    out->clear();
    for (size_t i = 0; i < std::size(kBudgetSpecials); ++i)
      if (text == kBudgetSpecials[i]) {
        out->push_back(500 + i);
        return q4t::Status{};
      }
    while (!text.empty()) {
      if (text.starts_with("<|image_pad|>")) {
        out->push_back(1000);
        text.remove_prefix(std::string_view("<|image_pad|>").size());
      } else {
        out->push_back(static_cast<unsigned char>(text.front()));
        text.remove_prefix(1);
      }
    }
    return q4t::Status{};
  };
  codec.decode = [](const std::vector<uint32_t>& ids, std::string* text) {
    if (ids.size() == 1 && ids[0] >= 500 && ids[0] < 504) {
      *text = kBudgetSpecials[ids[0] - 500];
      return q4t::Status{};
    }
    text->assign(ids.size(), 't');
    return q4t::Status{};
  };
  codec.expand_media =
      [](const Json& request, std::span<const VisionItem> items,
         const ChatPlanConfig& config, std::vector<uint32_t>* tokens,
         ChatPromptBudget* budget, ChatPlanError* error) {
        q4t::vision::MediaPlan media;
        return ExpandChatMedia(request, items, 1000, 1001, {}, {}, {48128},
                               config, tokens, &media, budget, error);
      };
  return codec;
}
BatchInputMeter Meter(ObjectStore& objects, BatchTokenCodec codec) {
  return [&objects, codec = std::move(codec)](
             std::string_view endpoint, const BatchInputRow& row,
             const Json& frozen, const ChatPlanConfig& config,
             const BinaryAccess& access, BatchInputBudget* budget) {
    return MeasureBatchInput(objects, endpoint, row, frozen, config, access,
                             codec, budget);
  };
}
Json Frozen(ObjectStore& objects, const Work& work,
            const ChatPlanConfig& config) {
  BatchPreparedInput prepared;
  if (!Ok(BatchPreparation(objects).Prepare(Access(), work.id, work.endpoint,
                                            work.row, config, &prepared)) ||
      !Publish(objects, work, &prepared))
    throw std::runtime_error("rate planner frozen fixture");
  Json frozen;
  if (!Ok(Load(objects, work, &frozen)))
    throw std::runtime_error("rate planner original inputs");
  return frozen;
}
}  // namespace

Q4T_TEST(batch_rate_plan_chat_choices_use_actual_context_capped_budget) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  auto request =
      Object({{"messages", Array({Object({{"role", String("user")},
                                          {"content", String("question")}})})},
              {"max_tokens", Number(32)},
              {"n", Number(3)}});
  auto work = Create(objects, request, "/v1/chat/completions");
  const auto frozen = Frozen(objects, work, fixture.config);
  BatchInputBudget input;
  BatchRateBudget rate;
  Q4T_CHECK(Ok(MeasureBatchInput(objects, work.endpoint, work.row, frozen,
                                 fixture.config, Access(), BudgetCodec(),
                                 &input, &rate)));
  Q4T_CHECK(input.exact && rate.input_exact &&
            rate.prompt_capacity == input.tokens && rate.output == 96 &&
            rate.auxiliary_output == 0 && rate.preparation_output == 0);
  fixture.config.max_len = input.tokens + 7;
  Q4T_CHECK(Ok(MeasureBatchInput(objects, work.endpoint, work.row, frozen,
                                 fixture.config, Access(), BudgetCodec(),
                                 &input, &rate)) &&
            rate.output == 21 && rate.Total() == input.tokens + 21);
  return true;
}

Q4T_TEST(batch_rate_plan_completion_charges_all_candidates_and_zero_output) {
  Fixture fixture;
  fixture.config.token_vocab = 2048;
  ObjectStore objects(fixture.options);
  for (int output : {0, 5}) {
    auto request = Object({{"model", String(fixture.config.model)},
                           {"prompt", Array({String("abc"), String("de")})},
                           {"max_tokens", Number(output)},
                           {"n", Number(2)},
                           {"best_of", Number(3)}});
    auto work = Create(objects, request, "/v1/completions");
    const auto frozen = Frozen(objects, work, fixture.config);
    BatchInputBudget input;
    BatchRateBudget rate;
    Q4T_CHECK(Ok(MeasureBatchInput(objects, work.endpoint, work.row, frozen,
                                   fixture.config, Access(), BudgetCodec(),
                                   &input, &rate)));
    Q4T_CHECK(rate.input_exact && rate.prompt_capacity == 5 &&
              rate.output == static_cast<uint64_t>(output * 6) &&
              rate.Total() == 5 + rate.output);
  }
  return true;
}

Q4T_TEST(
    batch_rate_plan_response_dynamic_steps_reserve_original_input_ceiling) {
  Fixture fixture;
  ClientLimits limits;
  limits.max_prompt_tokens = 2048;
  limits.max_output_tokens = 128;
  limits.max_body_bytes = 1 << 20;
  fixture.config.client_limits = &limits;
  ObjectStore objects(fixture.options);
  for (int mode = 0; mode < 5; ++mode) {
    auto request = Response("Record 42");
    Set(&request, "max_output_tokens", Number(32));
    if (mode == 1)
      Set(&request, "reasoning",
          Object({{"mode", String("pro")}, {"effort", String("none")}}));
    if (mode == 2)
      Set(&request, "reasoning",
          Object({{"summary", String("concise")}, {"effort", String("low")}}));
    if (mode == 3)
      Set(&request, "moderation",
          Object({{"model", String(fixture.config.model)}}));
    if (mode == 4)
      Set(&request, "context_management",
          Array({Object({{"type", String("compaction")},
                         {"compact_threshold", Number(4096)}})}));
    auto work = Create(objects, request);
    const auto frozen = Frozen(objects, work, fixture.config);
    BatchInputBudget input;
    BatchRateBudget rate;
    Q4T_CHECK(Ok(MeasureBatchInput(objects, work.endpoint, work.row, frozen,
                                   fixture.config, Access(), BudgetCodec(),
                                   &input, &rate)));
    Q4T_CHECK(input.exact && rate.input_exact == (mode == 0) &&
              rate.prompt_capacity == (mode ? 2048 : input.tokens) &&
              rate.output == 32 && rate.preparation_output == 0 &&
              rate.auxiliary_output == (mode == 3 ? 1024 : 0));
    const auto public_rows = Run(
        objects, Request(ObjectKind::kResponse, {}, ObjectOperation::kList));
    Q4T_CHECK(Ok(public_rows) &&
              public_rows.value.GetArray("data")->array.empty());
  }
  return true;
}

Q4T_TEST(batch_rate_plan_pending_sheet_retains_original_without_generation) {
  Fixture fixture;
  ClientLimits limits;
  limits.max_prompt_tokens = 4096;
  limits.max_output_tokens = 64;
  limits.max_body_bytes = 1 << 20;
  fixture.config.client_limits = &limits;
  ObjectStore objects(fixture.options);
  auto file = Upload(objects, "key,value\nalpha,42\n", "facts.csv");
  auto request = WithFile(file);
  Set(&request, "max_output_tokens", Number(32));
  auto work = Create(objects, request);
  const auto frozen = Frozen(objects, work, fixture.config);
  Q4T_CHECK(Ok(Files(objects).Erase(Access(), file)));
  auto codec = BudgetCodec();
  size_t encodes = 0;
  codec.encode = [&](std::string_view, std::vector<uint32_t>*) {
    ++encodes;
    return q4t::Status::Fail("No generated sheet summary exists");
  };
  BatchInputBudget input;
  BatchRateBudget rate;
  Q4T_CHECK(
      Ok(MeasureBatchInput(objects, work.endpoint, work.row, frozen,
                           fixture.config, Access(), codec, &input, &rate)));
  Q4T_CHECK(!input.exact && !rate.input_exact && rate.prompt_capacity == 4096 &&
            rate.output == 32 && rate.preparation_output == 2048 &&
            rate.auxiliary_output == 0 && encodes == 0);
  ResponsesExecution restored(objects);
  Q4T_CHECK(
      Ok(restored.RestoreInputs(frozen, fixture.config, State(), 42)) &&
      restored.PendingFileSheets() && !restored.Begun() &&
      restored.FileOutputBudget() == rate.preparation_output &&
      ResponsesState(objects).Retrieve(restored.Id(), State()).http_status ==
          404);
  return true;
}

Q4T_TEST(batch_rate_plan_prewarm_full_context_has_no_public_output) {
  Fixture fixture;
  fixture.config.prefix_cache = true;
  fixture.config.session_cache = true;
  ObjectStore objects(fixture.options);
  auto request = Response("a stable prefix");
  Set(&request, "max_output_tokens", Number(32));
  Set(&request, "prompt_cache_key", String("rate-fixture"));
  Set(&request, "prompt_cache_options", Object({{"prewarm", Boolean(true)}}));
  auto work = Create(objects, request);
  const auto frozen = Frozen(objects, work, fixture.config);
  BatchInputBudget input;
  BatchRateBudget rate;
  Q4T_CHECK(Ok(MeasureBatchInput(objects, work.endpoint, work.row, frozen,
                                 fixture.config, Access(), BudgetCodec(),
                                 &input, &rate)));
  fixture.config.max_len = input.tokens;
  Q4T_CHECK(Ok(MeasureBatchInput(objects, work.endpoint, work.row, frozen,
                                 fixture.config, Access(), BudgetCodec(),
                                 &input, &rate)) &&
            rate.output == 0 && rate.input_exact &&
            rate.Total() == input.tokens);
  return true;
}

Q4T_TEST(batch_rate_plan_restored_queued_identity_does_not_publish_or_start) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  auto request = Response("queued original");
  Set(&request, "background", Boolean(true));
  Set(&request, "max_output_tokens", Number(32));
  ResponsesExecution original(objects);
  Q4T_CHECK(Ok(original.Prepare(request, fixture.config, State(), 42, true)) &&
            original.BindRateDomain(ClientRateDomain::kBatch));
  original.BindBackgroundControl(
      std::make_shared<RequestControl>(original.Id(), "", Clock::now() + 30s));
  std::string wire;
  Q4T_CHECK(Ok(original.Begin(&wire)));
  const auto before = ResponsesState(objects).Retrieve(original.Id(), State());
  Q4T_CHECK(Ok(before));
  ResponsesExecution restored(objects);
  Q4T_CHECK(Ok(restored.RestoreQueued(original.Id(), fixture.config, State())));
  BatchRateBudget budget;
  Q4T_CHECK(Ok(MeasureBatchResponseRate(restored, fixture.config, Access(),
                                        BudgetCodec(), &budget)) &&
            budget.input_exact && budget.output == 32 &&
            budget.prompt_capacity > 0 &&
            restored.RateDomain() == ClientRateDomain::kBatch);
  const auto after = ResponsesState(objects).Retrieve(original.Id(), State());
  Q4T_CHECK(Ok(after) && OutputJson(before.value) == OutputJson(after.value) &&
            after.value.GetString("status") == "queued");
  return true;
}

Q4T_TEST(batch_rate_plan_failure_keeps_output_and_original_identity) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesExecution response(objects);
  auto request = Response("test input");
  Set(&request, "max_output_tokens", Number(32));
  Q4T_CHECK(Ok(response.Prepare(request, fixture.config, State(), 42)));
  for (int mode = 0; mode < 4; ++mode) {
    auto access = Access();
    auto config = fixture.config;
    auto codec = BudgetCodec();
    ClientLimits limits;
    if (mode == 0) access.scope = "client:other";
    if (mode == 1) access.deadline = Clock::now() - 1ms;
    if (mode == 2) access.cancelled = [] { return true; };
    if (mode == 3) {
      limits.max_prompt_tokens = 1;
      limits.max_output_tokens = 64;
      config.client_limits = &limits;
    }
    BatchRateBudget budget{77, 11, 5, 3, false};
    const auto result =
        MeasureBatchResponseRate(response, config, access, codec, &budget);
    Q4T_CHECK(!result.ok() && budget.prompt_capacity == 77 &&
              budget.output == 11 && budget.auxiliary_output == 5 &&
              budget.preparation_output == 3 && !budget.input_exact &&
              !response.Begun());
    if (mode == 1) Q4T_CHECK(result.http_status == 408);
    if (mode == 2) Q4T_CHECK(result.http_status == 499);
    if (mode == 3) Q4T_CHECK(result.code == "client_prompt_limit_exceeded");
  }
  return true;
}

Q4T_TEST(batch_budget_native_chat_and_responses_templates_persist_exact_count) {
  Fixture fixture;
  fixture.config.token_vocab = 2048;
  Work work;
  BatchInputBudget budget;
  {
    ObjectStore objects(fixture.options);
    std::string text;
    const auto meter = Meter(objects, BudgetCodec(&text));
    auto request =
        Object({{"messages", Array({Object({{"role", String("developer")},
                                            {"content", String("Read only")}}),
                                    Object({{"role", String("user")},
                                            {"content", String("查询 42")}})})},
                {"max_tokens", Number(16)}});
    auto chat = Create(objects, request, "/v1/chat/completions");
    BatchPreparedInput prepared;
    Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(
        Access(), chat.id, chat.endpoint, chat.row, fixture.config, &prepared,
        meter)));
    Q4T_CHECK(prepared.input_budget.exact &&
              prepared.input_budget.tokens == text.size() &&
              text.find("Read only") != text.npos &&
              text.find("查询 42") != text.npos &&
              Publish(objects, chat, &prepared));
    work = Create(objects, Response("frozen 42"));
    BatchPreparedInput response;
    Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(
        Access(), work.id, work.endpoint, work.row, fixture.config, &response,
        meter)));
    Q4T_CHECK(response.input_budget.exact &&
              response.input_budget.tokens == text.size() &&
              Publish(objects, work, &response));
    budget = response.input_budget;
    const auto root = BatchState(objects).Load(Access(), work.id);
    Q4T_CHECK(Ok(root) &&
              root.value.Find("data")->GetInt("enqueued_input_tokens") ==
                  static_cast<int64_t>(budget.tokens) &&
              root.value.Find("data")->GetInt("metered_rows") == 1);
  }
  ObjectStore objects(fixture.options);
  const auto row = BatchState(objects).Row(Access(), work.id, 0);
  BatchInputBudget restored;
  Q4T_CHECK(Ok(row) && row.value.Find("input_budget") &&
            ParseBatchInputBudget(*row.value.Find("input_budget"), &restored) &&
            restored.tokens == budget.tokens && restored.exact);
  return true;
}

Q4T_TEST(batch_budget_completion_fim_multi_prompt_raw_tokens_and_empty_seed) {
  Fixture fixture;
  fixture.config.token_vocab = 2048;
  ObjectStore objects(fixture.options);
  const auto meter = Meter(objects, BudgetCodec());
  for (int i = 0; i < 2; ++i) {
    auto request = Object(
        {{"prompt",
          i ? String("")
            : Array({Array({Number(11), Number(12)}), Array({Number(21)})})},
         {"max_tokens", Number(0)},
         {"echo", Boolean(true)}});
    Set(&request, "model", String(fixture.config.model));
    if (!i) {
      Set(&request, "suffix", String("ab"));
      Set(&request, "n", Number(2));
      Set(&request, "best_of", Number(2));
    }
    auto work = Create(objects, request, "/v1/completions");
    BatchPreparedInput prepared;
    Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(
        Access(), work.id, work.endpoint, work.row, fixture.config, &prepared,
        meter)));
    // Each prompt contributes its input once; choices do not multiply it.
    Q4T_CHECK(prepared.input_budget.exact &&
              prepared.input_budget.tokens == (i ? 1 : 13) &&
              Publish(objects, work, &prepared));
  }
  return true;
}

Q4T_TEST(batch_budget_visual_expansion_counts_actual_patch_tokens) {
  Fixture fixture;
  fixture.config.token_vocab = 2048;
  ObjectStore objects(fixture.options);
  const auto request = Object(
      {{"messages",
        Array({Object(
            {{"role", String("user")},
             {"content",
              Array({Object(
                  {{"type", String("image_url")},
                   {"image_url",
                    Object({{"url", String("data:image/png;base64,"
                                           "iVBORw0KGgoAAAANSUhEUgAAARAAAAEQCAI"
                                           "AAAD3aLxC")}})}})})}})})}});
  auto work = Create(objects, request, "/v1/chat/completions");
  std::string text;
  auto codec = BudgetCodec(&text);
  BatchPreparedInput prepared;
  Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(
      Access(), work.id, work.endpoint, work.row, fixture.config, &prepared,
      Meter(objects, codec))));
  std::vector<uint32_t> base;
  Q4T_CHECK(codec.encode(text, &base));
  Q4T_CHECK(prepared.input_budget.exact &&
            prepared.input_budget.tokens == base.size() - 1 + 64);
  codec.expand_media = {};
  BatchInputBudget unchanged{123, false};
  Q4T_CHECK(Publish(objects, work, &prepared));
  Json frozen;
  Q4T_CHECK(Ok(Load(objects, work, &frozen)));
  const auto failure =
      MeasureBatchInput(objects, work.endpoint, work.row, frozen,
                        fixture.config, Access(), codec, &unchanged);
  Q4T_CHECK(failure.code == "batch_media_count_unavailable" &&
            unchanged.tokens == 123 && !unchanged.exact);
  return true;
}

Q4T_TEST(
    batch_budget_pending_csv_reserves_context_without_model_or_fake_count) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto file = Upload(objects, "name,value\nalpha,42\n", "facts.csv");
  auto work = Create(objects, WithFile(file));
  ClientLimits limits;
  limits.max_prompt_tokens = 4096;
  limits.max_output_tokens = 2048;
  limits.max_body_bytes = 1 << 20;
  fixture.config.client_limits = &limits;
  auto codec = BudgetCodec();
  size_t encoded = 0;
  codec.encode = [&](std::string_view, std::vector<uint32_t>*) {
    ++encoded;
    return q4t::Status::Fail("no model summary or final prompt exists yet");
  };
  BatchPreparedInput prepared;
  Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(
      Access(), work.id, work.endpoint, work.row, fixture.config, &prepared,
      Meter(objects, codec))));
  Q4T_CHECK(!prepared.input_budget.exact &&
            prepared.input_budget.tokens == 4096 && encoded == 0 &&
            Publish(objects, work, &prepared));
  const auto public_responses =
      Run(objects, Request(ObjectKind::kResponse, {}, ObjectOperation::kList));
  Q4T_CHECK(Ok(public_responses) &&
            public_responses.value.GetArray("data")->array.empty());
  return true;
}

Q4T_TEST(
    batch_budget_cancellation_limits_and_malformed_counts_are_transactional) {
  Fixture fixture;
  fixture.config.token_vocab = 2048;
  ObjectStore objects(fixture.options);
  auto work = Create(objects,
                     Object({{"prompt", String("abcdef")},
                             {"max_tokens", Number(1)},
                             {"model", String(fixture.config.model)}}),
                     "/v1/completions");
  const auto frozen = Object({{"kind", String("completion")}});
  for (int i = 0; i < 4; ++i) {
    auto codec = BudgetCodec();
    auto access = Access();
    auto config = fixture.config;
    bool cancelled = false;
    if (i == 0) access.deadline = Clock::now() - 1ms;
    if (i == 1) {
      access.cancelled = [&] { return cancelled; };
      codec.encode = [&](std::string_view, std::vector<uint32_t>* tokens) {
        cancelled = true;
        tokens->assign(6, 1);
        return q4t::Status{};
      };
    }
    if (i == 2) config.max_len = 4;
    if (i == 3)
      codec.encode = [](std::string_view, std::vector<uint32_t>*) {
        return q4t::Status::Fail("CPU codec failure");
      };
    BatchInputBudget result{77, false};
    auto reply = MeasureBatchInput(objects, work.endpoint, work.row, frozen,
                                   config, access, codec, &result);
    Q4T_CHECK(!reply.ok() && result.tokens == 77 && !result.exact);
    if (i == 0) Q4T_CHECK(reply.http_status == 408);
    if (i == 1) Q4T_CHECK(reply.http_status == 499);
    if (i == 2) Q4T_CHECK(reply.code == "context_length_exceeded");
  }
  for (int i = 0; i < 5; ++i) {
    auto value = BatchInputBudgetJson({1, true});
    if (i == 0) Set(&value, "tokens", Number(0));
    if (i == 1) Set(&value, "tokens", Number(kBatchTokenLimit + 1));
    if (i == 2) Set(&value, "tokens", Boolean(true));
    if (i == 3) Set(&value, "mode", String("estimated_as_exact"));
    if (i == 4) Set(&value, "extra", Number(1));
    BatchInputBudget result{91, false};
    Q4T_CHECK(!ParseBatchInputBudget(value, &result) && result.tokens == 91 &&
              !result.exact);
  }
  return true;
}

Q4T_TEST(batch_budget_validation_restart_reuses_original_metered_prefix) {
  Fixture fixture;
  fixture.config.token_vocab = 2048;
  ValidationWork work;
  uint64_t first = 0;
  {
    ObjectStore objects(fixture.options);
    work = ValidationInput(objects, {Response("first"), Response("second")});
    BatchValidation validation(objects, work.id, fixture.config,
                               Meter(objects, BudgetCodec()));
    Q4T_CHECK(Ok(validation.Step(Access())) && !validation.Complete());
    const auto root = BatchState(objects).Load(Access(), work.id);
    Q4T_CHECK(Ok(root));
    first = root.value.Find("data")->GetInt("enqueued_input_tokens");
    Q4T_CHECK(first > 0 &&
              root.value.Find("data")->GetInt("metered_rows") == 1);
  }
  ObjectStore objects(fixture.options);
  size_t metered = 0;
  auto actual = Meter(objects, BudgetCodec());
  BatchInputMeter meter =
      [&](std::string_view endpoint, const BatchInputRow& row,
          const Json& frozen, const ChatPlanConfig& config,
          const BinaryAccess& access, BatchInputBudget* output) {
        ++metered;
        if (row.line != 2)
          return Error(500, "recounted", "recounted frozen prefix");
        return actual(endpoint, row, frozen, config, access, output);
      };
  BatchValidation validation(objects, work.id, fixture.config, meter);
  for (int i = 0; i < 5 && !validation.Complete(); ++i)
    Q4T_CHECK(Ok(validation.Step(Access())));
  const auto root = BatchState(objects).Load(Access(), work.id);
  const auto second = BatchState(objects).Row(Access(), work.id, 1);
  BatchInputBudget count;
  Q4T_CHECK(validation.Complete() && metered == 1 && Ok(root) && Ok(second) &&
            second.value.Find("input_budget") &&
            ParseBatchInputBudget(*second.value.Find("input_budget"), &count) &&
            root.value.Find("data")->GetInt("metered_rows") == 2 &&
            root.value.Find("data")->GetInt("enqueued_input_tokens") ==
                static_cast<int64_t>(first + count.tokens));
  return true;
}

Q4T_TEST(
    batch_budget_responses_context_truncation_uses_selected_native_prompt) {
  Fixture fixture;
  fixture.config.token_vocab = 2048;
  // Byte tokens include the native refusal instruction as well as messages.
  // Keep a real fitting suffix and an oversized old turn.
  fixture.config.max_len = 1024;
  ObjectStore objects(fixture.options);
  auto request = Object(
      {{"input", Array({Object({{"role", String("user")},
                                {"content", String(std::string(2000, 'o'))}}),
                        Object({{"role", String("assistant")},
                                {"content", String("old answer")}}),
                        Object({{"role", String("user")},
                                {"content", String("retain 42")}})})},
       {"max_output_tokens", Number(16)},
       {"truncation", String("auto")},
       {"reasoning", Object({{"effort", String("none")}})}});
  auto work = Create(objects, request);
  std::string encoded;
  BatchPreparedInput prepared;
  Q4T_CHECK(Ok(BatchPreparation(objects).Prepare(
      Access(), work.id, work.endpoint, work.row, fixture.config, &prepared,
      Meter(objects, BudgetCodec(&encoded)))));
  Q4T_CHECK(prepared.input_budget.exact &&
            prepared.input_budget.tokens == encoded.size() &&
            prepared.input_budget.tokens < 1024 &&
            encoded.find("retain 42") != encoded.npos &&
            encoded.find(std::string(50, 'o')) == encoded.npos);
  return true;
}

Q4T_TEST(batch_recount_native_frozen_file_prefix_resumes_without_fetch) {
  Fixture fixture;
  fixture.config.token_vocab = 2048;
  ValidationWork work;
  Json original;
  {
    ObjectStore objects(fixture.options);
    const auto file = Upload(objects, "original recount fact 42", "fact.txt");
    work = ValidationInput(
        objects, {WithFile(file), Response("second"), Response("new")});
    BatchValidation old(objects, work.id, fixture.config);
    Q4T_CHECK(Ok(old.Step(Access())) && Ok(old.Step(Access())) &&
              !old.Complete());
    original = BatchState(objects).Row(Access(), work.id, 0).value;
    Q4T_CHECK(Ok(Files(objects).Erase(Access(), file)) &&
              Ok(Files(objects).Erase(Access(), work.file)));
    BatchValidation recount(objects, work.id, fixture.config,
                            Meter(objects, BudgetCodec()));
    Q4T_CHECK(Ok(recount.Step(Access())) && recount.RowsChecked() == 0);
    const auto root = BatchState(objects).Load(Access(), work.id);
    Q4T_CHECK(Ok(root) &&
              root.value.Find("data")->Find("input_recount")->GetInt("rows") ==
                  1 &&
              !root.value.Find("data")->Has("enqueued_input_tokens"));
  }
  ObjectStore objects(fixture.options);
  auto meter = Meter(objects, BudgetCodec());
  size_t measured = 0;
  BatchInputMeter remaining = [&](auto endpoint, const auto& row,
                                  const auto& prepared, const auto& config,
                                  const auto& access, auto* count) {
    ++measured;
    if (row.line == 1)
      return Error(500, "counted_again", "Recovered count was repeated");
    return meter(endpoint, row, prepared, config, access, count);
  };
  BatchValidation restored(objects, work.id, fixture.config, remaining);
  for (int i = 0; i < 6 && !restored.Complete(); ++i)
    Q4T_CHECK(Ok(restored.Step(Access())));
  Q4T_CHECK(restored.Complete() && measured == 2);
  auto row = BatchState(objects).Row(Access(), work.id, 0);
  Q4T_CHECK(Ok(row) && row.value.Has("input_budget"));
  std::erase_if(row.value.object, [](const auto& field) {
    return field.first == "input_budget";
  });
  Q4T_CHECK(OutputJson(row.value) == OutputJson(original));
  const auto root = BatchState(objects).Load(Access(), work.id);
  Q4T_CHECK(Ok(root) && root.value.Find("data")->GetInt("metered_rows") == 3 &&
            root.value.Find("data")->Find("input_recount")->GetInt("rows") ==
                2);
  return true;
}

Q4T_TEST(batch_recount_actual_partial_lost_ack_resumes_next_original_row) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 150;
  ValidationWork work;
  {
    ObjectStore objects(fixture.options);
    work = ValidationInput(objects, {Response("first"), Response("second")});
    BatchValidation old(objects, work.id, fixture.config);
    Q4T_CHECK(Ok(old.Step(Access())) && Ok(old.Step(Access())) &&
              old.Complete());
  }
  const auto fixed = [](uint64_t amount, size_t expected) -> BatchInputMeter {
    return [=](const auto&, const BatchInputRow& row, const auto&, const auto&,
               const auto&, BatchInputBudget* budget) {
      if (row.line != expected)
        return Error(500, "wrong_recount_row", "Repeated or skipped row");
      *budget = {amount, true};
      return ObjectReply{};
    };
  };
  ObjectWorkerEntryForTesting("--q4t-object-worker-batch-recount-lost-ack");
  ScopeExit reset([] { ObjectWorkerEntryForTesting({}); });
  {
    ObjectStore objects(fixture.options);
    const auto reply = RecountBatchInput(objects, Access(), work.id,
                                         fixture.config, fixed(7, 1));
    Q4T_CHECK(!reply.ok() && reply.commit_unknown);
  }
  ObjectWorkerEntryForTesting({});
  ObjectStore objects(fixture.options);
  Q4T_CHECK(Ok(RecountBatchInput(objects, Access(), work.id, fixture.config,
                                 fixed(9, 2))));
  const auto root = BatchState(objects).Load(Access(), work.id);
  Q4T_CHECK(Ok(root) &&
            root.value.Find("data")->GetInt("enqueued_input_tokens") == 16 &&
            root.value.Find("data")->GetInt("metered_rows") == 2);
  return true;
}

Q4T_TEST(batch_recount_scope_model_deadline_and_cancel_prevent_publication) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto work = ValidationInput(objects, {Response("first")});
  BatchValidation old(objects, work.id, fixture.config);
  Q4T_CHECK(Ok(old.Step(Access())) && old.Complete());
  size_t counted = 0;
  bool cancelled = false;
  BatchInputMeter meter = [&](const auto&, const auto&, const auto&,
                              const auto&, const auto&,
                              BatchInputBudget* result) {
    ++counted;
    cancelled = true;
    *result = {4, true};
    return ObjectReply{};
  };
  for (int i = 0; i < 5; ++i) {
    auto access = Access();
    auto config = fixture.config;
    if (i == 0) access.scope = "client:foreign";
    if (i == 1) config.model = "changed-model";
    if (i == 2) access.deadline = Clock::now();
    if (i == 3) access.cancelled = [] { return true; };
    if (i == 4) access.cancelled = [&] { return cancelled; };
    Q4T_CHECK(!RecountBatchInput(objects, access, work.id, config, meter).ok());
  }
  const auto root = BatchState(objects).Load(Access(), work.id);
  Q4T_CHECK(counted == 1 && Ok(root) &&
            !root.value.Find("data")->Has("input_recount") &&
            !root.value.Find("data")->Has("enqueued_input_tokens"));
  return true;
}

Q4T_TEST(batch_recount_completed_inline_result_and_ticket_are_immutable) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto work =
      ValidationInput(objects, {Response("first"), Response("second")});
  BatchValidation old(objects, work.id, fixture.config);
  Q4T_CHECK(Ok(old.Step(Access())) && Ok(old.Step(Access())));
  BatchState batches(objects);
  const auto started = batches.StartRow(Access(), work.id, 0);
  Q4T_CHECK(Ok(started));
  BatchInputRow first;
  Q4T_CHECK(Ok(batches.ReadInputRow(Access(), work.id, 0, &first)));
  BatchRowResult result;
  result.request_id = started.value.GetString("request_id");
  result.http_status = 200;
  result.response_body = R"({"output":[],"usage":{"input_tokens":7}})";
  Q4T_CHECK(
      Ok(batches.FinishRow(Access(), work.id, 0, first.custom_id, result)));
  const auto before = batches.Row(Access(), work.id, 0);
  Q4T_CHECK(Ok(before) && before.value.Has("result_inline"));
  Q4T_CHECK(Ok(batches.RecountInput(Access(), work.id, 0, {7, true})) &&
            Ok(batches.RecountInput(Access(), work.id, 1, {8, false})));
  auto after = batches.Row(Access(), work.id, 0);
  Q4T_CHECK(Ok(after) && after.value.Has("input_budget"));
  std::erase_if(after.value.object, [](const auto& field) {
    return field.first == "input_budget";
  });
  Q4T_CHECK(OutputJson(before.value) == OutputJson(after.value));
  const auto root = batches.Load(Access(), work.id);
  Q4T_CHECK(Ok(root) && root.value.Find("data")->GetInt("next_row") == 1 &&
            root.value.Find("data")->GetInt("enqueued_input_tokens") == 15);
  return true;
}

Q4T_TEST(batch_recount_partial_budget_cannot_move_model_and_can_cancel) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto work = ValidationInput(
      objects, {Response("first"), Response("second"), Response("later")});
  BatchValidation old(objects, work.id, fixture.config);
  Q4T_CHECK(
      Ok(old.Step(Access())) && Ok(old.Step(Access())) &&
      Ok(BatchState(objects).RecountInput(Access(), work.id, 0, {7, true})));
  auto root = BatchState(objects).Load(Access(), work.id);
  Q4T_CHECK(Ok(root));
  for (int i = 0; i < 3; ++i) {
    auto put = Request(ObjectKind::kBatch, work.id, ObjectOperation::kPut);
    put.expected_revision = root.value.GetInt("revision");
    put.created_at = root.value.GetInt("created_at");
    put.expires_at = root.value.GetInt("expires_at");
    put.data = *root.value.Find("data");
    if (i == 0) Set(&put.data, "canonical_model", String("quota-escape"));
    if (i == 1) Set(&put.data, "pages", Array());
    if (i == 2) {
      Set(&Field(put.data, "batch"), "status", String("completed"));
      Set(&put.data, "resources_released", Boolean(true));
    }
    Q4T_CHECK(Run(objects, put).code == "batch_budget_conflict");
  }
  BatchState batches(objects);
  Q4T_CHECK(Ok(batches.Cancel(Access(), work.id)) &&
            Ok(batches.FinishCancelled(Access(), work.id)));
  root = batches.Load(Access(), work.id);
  Q4T_CHECK(Ok(root) &&
            root.value.Find("data")->Find("batch")->GetString("status") ==
                "cancelled" &&
            root.value.Find("data")->Find("input_recount")->GetInt("tokens") ==
                7);
  return true;
}

Q4T_TEST(batch_moderations_freezes_original_and_counts_all_classifier_steps) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto request = Object({{"input", Array({String("first actual input"),
                                                 String("second input")})}});
  const auto work = Create(objects, request, "/v1/moderations");
  const auto frozen = Frozen(objects, work, fixture.config);
  Q4T_CHECK(frozen.GetString("kind") == "moderation");
  BatchInputBudget input;
  BatchRateBudget rate;
  std::string encoded;
  Q4T_CHECK(Ok(MeasureBatchInput(objects, work.endpoint, work.row, frozen,
                                 fixture.config, Access(), BudgetCodec(&encoded),
                                 &input, &rate)));
  Q4T_CHECK(input.exact && rate.input_exact && rate.prompt_capacity == input.tokens &&
            rate.output == 0 && rate.auxiliary_output == 0 &&
            rate.preparation_output == 1024 && rate.Total() == input.tokens + 1024);
  Q4T_CHECK(encoded.find("second input") != encoded.npos &&
            encoded.find("first actual input") == encoded.npos);
  uint64_t total = 0;
  for (const auto& text : request.GetArray("input")->array) {
    const auto one = Create(objects, Object({{"input", text}}), work.endpoint);
    const auto single = Frozen(objects, one, fixture.config);
    BatchInputBudget count;
    Q4T_CHECK(Ok(MeasureBatchInput(objects, one.endpoint, one.row, single,
                                   fixture.config, Access(), BudgetCodec(), &count)));
    total += count.tokens;
  }
  Q4T_CHECK(total == input.tokens);
  auto changed = work;
  Set(&changed.row.body, "input", String("different request"));
  Json altered;
  Q4T_CHECK(!Load(objects, changed, &altered).ok());
  return true;
}

Q4T_TEST(batch_moderations_rejects_cumulative_input_limit_and_wrong_kind) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto work = Create(objects, Object({{"input", Array({String("one"),
                                                               String("two")})}}),
                           "/v1/moderations");
  const auto frozen = Frozen(objects, work, fixture.config);
  ClientLimits limits;
  limits.max_output_tokens = 1;
  limits.max_prompt_tokens = 1;
  fixture.config.client_limits = &limits;
  BatchInputBudget input;
  BatchRateBudget rate;
  auto result = MeasureBatchInput(objects, work.endpoint, work.row, frozen,
                                   fixture.config, Access(), BudgetCodec(),
                                   &input, &rate);
  Q4T_CHECK(result.code == "client_prompt_limit_exceeded");
  limits.max_prompt_tokens = 8192;
  Q4T_CHECK(Ok(MeasureBatchInput(objects, work.endpoint, work.row, frozen,
                                 fixture.config, Access(), BudgetCodec(),
                                 &input, &rate)));
  Q4T_CHECK(rate.preparation_output == 1024 && rate.output == 0);
  auto wrong = frozen;
  Set(&wrong, "kind", String("completion"));
  Q4T_CHECK(MeasureBatchInput(objects, work.endpoint, work.row, wrong,
                              fixture.config, Access(), BudgetCodec(),
                              &input, &rate).code == "batch_budget_invalid");
  return true;
}

Q4T_TEST(batch_native_embedding_rejects_mixed_models_without_relabeling) {
  Fixture fixture;
  fixture.config.native_embedding_enabled = true;
  fixture.config.token_vocab = 256;
  fixture.config.embedding_dimensions = 2560;
  ObjectStore objects(fixture.options);
  std::string raw;
  for (const auto* name : {"qwen3-embedding-0.6b", "cpu-alias"}) {
    raw += OutputJson(Object({{"custom_id", String(name)},
        {"method", String("POST")}, {"url", String("/v1/embeddings")},
        {"body", Object({{"model", String(name)},
                         {"input", Array({Number(0)})}})}})) + '\n';
  }
  const auto file = Upload(objects, raw, "mixed.jsonl", "batch");
  const auto created = BatchState(objects).Create(
      Access(), {file, "/v1/embeddings", Object()});
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  BatchValidation validation(objects, id, fixture.config,
                              Meter(objects, BudgetCodec()));
  ObjectReply reply;
  for (int i = 0; i < 6 && reply.ok() && !validation.Complete(); ++i)
    reply = validation.Step(Access());
  Q4T_CHECK(!reply.ok() && reply.http_status == 400 &&
            reply.value.GetString("param") == "body.model");
  const auto root = BatchState(objects).Load(Access(), id);
  Q4T_CHECK(Ok(root) && root.value.Find("data")->GetString("canonical_model")
            == "qwen3-embedding-0.6b" &&
            root.value.Find("data")->GetInt("validated_rows") == 1);
  return true;
}

Q4T_TEST(batch_native_embedding_canonical_recount_survives_restart) {
  Fixture fixture;
  fixture.config.native_embedding_enabled = true;
  fixture.config.token_vocab = 256;
  fixture.config.embedding_dimensions = 2560;
  fixture.config.max_len = 1;
  fixture.config.capacities = {1};
  Work work;
  Json original;
  {
    ObjectStore objects(fixture.options);
    work = Create(objects, Object({{"model", String("qwen3-embedding-0.6b")},
        {"input", Array({Number(0), Number(151643)})}}), "/v1/embeddings");
    BatchValidation validation(objects, work.id, fixture.config);
    for (int i = 0; i < 6 && !validation.Complete(); ++i)
      Q4T_CHECK(Ok(validation.Step(Access())));
    Q4T_CHECK(validation.Complete());
    const auto root = BatchState(objects).Load(Access(), work.id);
    Q4T_CHECK(Ok(root) && root.value.Find("data")->GetString("canonical_model")
              == "qwen3-embedding-0.6b");
    original = BatchState(objects).Row(Access(), work.id, 0).value;
    Q4T_CHECK(Ok(Files(objects).Erase(Access(), work.file)));
  }
  ObjectStore objects(fixture.options);
  auto codec = BudgetCodec();
  codec.encode = [](auto, auto*) {
    return q4t::Status::Fail("Raw IDs must not be tokenized");
  };
  auto underlying = Meter(objects, codec);
  bool selected = false;
  BatchInputMeter meter = [&](auto endpoint, const auto& row, const auto& frozen,
                              const auto& config, const auto& access, auto* out) {
    selected = config.model == "qwen3-embedding-0.6b" &&
               config.token_vocab == 151669 && config.max_len == 8192 &&
               config.capacities.empty();
    return underlying(endpoint, row, frozen, config, access, out);
  };
  Q4T_CHECK(Ok(RecountBatchInput(objects, Access(), work.id, fixture.config,
                                 meter)) && selected);
  const auto root = BatchState(objects).Load(Access(), work.id);
  Q4T_CHECK(Ok(root) && root.value.Find("data")->GetInt("enqueued_input_tokens")
            == 2 && root.value.Find("data")->GetString("canonical_model")
            == "qwen3-embedding-0.6b");
  auto row = BatchState(objects).Row(Access(), work.id, 0);
  Q4T_CHECK(Ok(row) && row.value.Find("input_budget")->GetInt("tokens") == 2);
  std::erase_if(row.value.object, [](const auto& field) {
    return field.first == "input_budget";
  });
  Q4T_CHECK(OutputJson(row.value) == OutputJson(original));
  return true;
}

Q4T_TEST(batch_embeddings_freeze_raw_inputs_meter_and_restore_original) {
  Fixture fixture;
  fixture.config.token_vocab = 2048;
  fixture.config.embedding_dimensions = 2560;
  Work work;
  {
    ObjectStore objects(fixture.options);
    work = Create(objects, Object({{"model", String("cpu-alias")},
          {"input", Array({String("first"), String("second")})}}),
          "/v1/embeddings");
    const auto frozen = Frozen(objects, work, fixture.config);
    Q4T_CHECK(frozen.GetString("kind") == "embedding");
    BatchInputBudget input;
    BatchRateBudget rate;
    std::string last;
    Q4T_CHECK(Ok(MeasureBatchInput(objects, work.endpoint, work.row, frozen,
        fixture.config, Access(), BudgetCodec(&last), &input, &rate)));
    Q4T_CHECK(input.tokens == 11 && input.exact && last == "second" &&
        rate.Total() == 11 && rate.output == 0 && !rate.preparation_output);
  }
  ObjectStore objects(fixture.options);
  Json restored;
  Q4T_CHECK(Ok(Load(objects, work, &restored)) &&
            restored.GetString("kind") == "embedding");
  auto changed = work;
  Set(&changed.row.body, "input", String("different"));
  Json invalid;
  Q4T_CHECK(!Load(objects, changed, &invalid).ok());
  ClientLimits limits;
  limits.max_prompt_tokens = 10;
  fixture.config.client_limits = &limits;
  BatchInputBudget input;
  Q4T_CHECK(MeasureBatchInput(objects, work.endpoint, work.row, restored,
        fixture.config, Access(), BudgetCodec(), &input).code ==
        "client_prompt_limit_exceeded");
  return true;
}

Q4T_TEST(batch_embeddings_large_finite_result_survives_restart_and_recount) {
  Fixture fixture;
  fixture.config.token_vocab = 2048;
  fixture.config.embedding_dimensions = 2560;
  Work work;
  std::string original;
  {
    ObjectStore objects(fixture.options);
    Json inputs = Array(); inputs.array.assign(32, String("a"));
    const auto request = Object({{"model", String("cpu-alias")},
                                 {"input", inputs}});
    work = Create(objects, request, "/v1/embeddings");
    BatchValidation validation(objects, work.id, fixture.config,
                                Meter(objects, BudgetCodec()));
    for (int i = 0; i < 3 && !validation.Complete(); ++i)
      Q4T_CHECK(Ok(validation.Step(Access())));
    Q4T_CHECK(validation.Complete());
    EmbeddingsRequestPlan plan;
    ChatPlanError error;
    Q4T_CHECK(PlanEmbeddingsRequest(request, fixture.config, &plan, &error));
    std::vector<std::vector<float>> vectors(32, std::vector<float>(2560));
    for (auto& vector : vectors) vector[0] = 1;
    Q4T_CHECK(BuildEmbeddingsResponse(plan, vectors, 32, &original));
    BatchState batches(objects);
    auto started = batches.StartRow(Access(), work.id, 0);
    Q4T_CHECK(Ok(started));
    BatchRowResult result;
    result.request_id = started.value.GetString("request_id");
    result.http_status = 200;
    result.response_body = original;
    Q4T_CHECK(Ok(batches.FinishRow(Access(), work.id, 0,
                                    work.row.custom_id, result)));
    auto root = batches.Load(Access(), work.id);
    Q4T_CHECK(Ok(root) && root.value.Find("data")->Find("batch")
                  ->Find("usage")->GetInt("input_tokens") == 32);
    // Simulate a pre-accounting checkpoint; original result bytes are the
    // only source for recovery. No endpoint executor is installed here.
    auto changed = Request(ObjectKind::kBatch, work.id, ObjectOperation::kPut);
    changed.expected_revision = root.value.GetInt("revision");
    changed.created_at = root.value.GetInt("created_at");
    changed.expires_at = root.value.GetInt("expires_at");
    changed.data = *root.value.Find("data");
    std::erase_if(changed.data.object, [](const auto& field) {
      return field.first == "usage_accounting";
    });
    std::erase_if(Field(changed.data, "batch").object, [](const auto& field) {
      return field.first == "usage";
    });
    Q4T_CHECK(Ok(Run(objects, changed)));
  }
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  Q4T_CHECK(Ok(batches.RecountUsage(Access(), work.id)));
  const auto root = batches.Load(Access(), work.id);
  Q4T_CHECK(Ok(root) && root.value.Find("data")->Find("batch")
                ->Find("usage")->GetInt("input_tokens") == 32 &&
            root.value.Find("data")->Find("batch")->Find("usage")
                ->GetInt("output_tokens") == 0);
  Q4T_CHECK(Ok(batches.BeginFinalization(Access(), work.id, "completed")));
  const auto final = batches.Finalize(Access(), work.id);
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "completed");
  BinaryRead read;
  Files files(objects);
  Q4T_CHECK(Ok(files.Open(Access(), final.value.GetString("output_file_id"), &read)));
  std::string content;
  for (size_t i = 0; i < read.manifest.chunks.size(); ++i) {
    std::string bytes;
    Q4T_CHECK(Ok(BinaryStore(objects).Read(Access(), read, i, &bytes)));
    content += bytes;
  }
  Q4T_CHECK(content.find(original) != content.npos);
  Q4T_CHECK(Ok(BinaryStore(objects).Close(Access(), &read)));
  Json metadata;
  Q4T_CHECK(ParseEmbeddingMetadata(content, &metadata, true) &&
            metadata.GetString("custom_id") == work.row.custom_id);
  return true;
}
