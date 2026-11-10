#include "q4t/server/batch_preparation.h"

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
bool Publish(ObjectStore& objects, const Work& work,
               BatchPreparedInput* prepared) {
  BatchRowPosition position{work.row.line, work.row.offset, work.row.wire_bytes,
                             Digest(work.row.custom_id), prepared->reference,
                             prepared->binding_sha256};
  auto result = BatchState(objects).AppendValidationPage(
      Access(), work.id, std::span(&position, 1));
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
  auto work = Create(objects, Response());
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
    work = Create(objects, Response());
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
