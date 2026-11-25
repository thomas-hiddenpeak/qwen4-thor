#include "q4t/server/evals.h"
#include "q4t/server/eval_dispatcher.h"
#include "q4t/model/embedding_config.h"

#include <atomic>
#include <cmath>
#include <cstdlib>
#include <thread>
#include <filesystem>

#include "../src/server/eval_grading.h"
#include "../src/server/eval_runs.h"
#include "../src/server/eval_sampling.h"
#include "../src/server/eval_trajectory.h"
#include "../src/server/object_protocol.h"
#include "q4t/server/http_connection.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/chat_file_inputs.h"
#include "q4t/server/file_input.h"
#include "q4t/server/http_request.h"
#include "q4t/server/chat_storage.h"
#include "q4t/server/files.h"
#include "q4t/test.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace std::chrono_literals;

struct Fixture {
  std::filesystem::path directory;
  ObjectStoreOptions options;
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto base = std::filesystem::absolute(output ? output : ".q4t-work/evals");
    std::filesystem::create_directories(base);
    auto pattern = (base / "eval-XXXXXX").string();
    if (!mkdtemp(pattern.data())) throw std::runtime_error("Eval fixture");
    directory = pattern;
    options.directory = (directory / "objects").string();
  }
  ~Fixture() { std::filesystem::remove_all(directory); }
};

ObjectReply Request(ObjectStore& store, std::string_view method,
                    std::string path, std::string body = {},
                    std::string scope = "client:alpha") {
  StateAccess access{
      std::move(scope), std::chrono::steady_clock::now() + 10s, {}};
  return HandleEval(store, access, method, path, body);
}

const char* kDefinition = R"({
  "name":"Scope report",
  "data_source_config":{"type":"custom","item_schema":{
    "type":"object","properties":{"count":{"type":"integer"}},"required":["count"]},
    "include_sample_schema":true},
  "testing_criteria":[{"name":"Count check","type":"string_check","input":"{{sample.output_text}}",
    "operation":"eq","reference":"{{item.count}}"}],
  "metadata":{"project":"q4t"}})";

Q4T_TEST(evals_stored_completion_snapshot_filters_and_survives_source_deletion) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  StateAccess alpha{"client:alpha", std::chrono::steady_clock::now() + 10s, {}};
  StateAccess beta{"client:beta", alpha.deadline, {}};
  auto definition = Request(store, "POST", "/v1/evals",
      R"({"data_source_config":{"type":"logs","metadata":{"project":"q4t"}},"testing_criteria":[]})");
  Q4T_CHECK(definition.ok());
  const auto path = "/v1/evals/" + definition.value.GetString("id") + "/runs";
  auto save = [&](const StateAccess& access, std::string id, std::string project,
                  std::string version) {
    auto input = Object({{"store", Boolean(true)},
                         {"metadata", Object({{"project", String(project)},
                                              {"version", String(version)}})},
                         {"messages", Array({Object({{"role", String("user")},
                                                      {"content", String("Read project")}})})}});
    auto chat = Object({{"object", String("chat.completion")}, {"id", String(id)},
                        {"model", String("cpu")}, {"created", Number(std::time(nullptr))},
                        {"choices", Array({Object({{"finish_reason", String("stop")},
                           {"message", Object({{"role", String("assistant")},
                                               {"content", String(id)}})}})})}});
    return StoredChats(store).Save(input, chat, access);
  };
  Q4T_CHECK(save(alpha, "chatcmpl_original", "q4t", "new").ok());
  Q4T_CHECK(save(beta, "chatcmpl_foreign", "q4t", "new").ok());
  Q4T_CHECK(save(alpha, "chatcmpl_old", "q4t", "old").ok());
  Q4T_CHECK(save(alpha, "chatcmpl_elsewhere", "different", "new").ok());
  auto run = Request(store, "POST", path, R"({"data_source":{"type":"completions","model":"cpu",
      "source":{"type":"stored_completions","metadata":{"version":"new"},"model":"cpu","limit":2}}})");
  Q4T_CHECK(run.ok());
  Q4T_CHECK(StoredChats(store).Erase("chatcmpl_original", alpha).ok());
  BinaryAccess auth{alpha.scope, alpha.deadline, {}};
  BinaryRead read;
  BinaryStore binary(store);
  Q4T_CHECK(binary.Open(auth, ObjectKind::kEvalRun, run.value.GetString("id"), 60, &read).ok());
  Q4T_CHECK(read.manifest.chunks.size() == 1);
  std::string bytes;
  Q4T_CHECK(binary.Read(auth, read, 0, &bytes).ok());
  Q4T_CHECK(binary.Close(auth, &read).ok());
  Json row;
  Q4T_CHECK(io::ParseJson(bytes, &row, true));
  const auto& item = *row.Find("item");
  Q4T_CHECK(item.GetString("output_text") == "chatcmpl_original");
  Q4T_CHECK(item.GetArray("input_trajectory")->array[0].GetString("content") == "Read project");
  Q4T_CHECK(item.Find("metadata")->GetString("project") == "q4t");
  Q4T_CHECK(bytes.find("chatcmpl_foreign") == std::string::npos);
  Q4T_CHECK(Request(store, "DELETE", path + "/" + run.value.GetString("id")).ok());
  return true;
}

Q4T_TEST(
    evals_chat_image_projection_preserves_source_detail_and_cache_boundary) {
  for (
      const auto* input :
      {R"({"type":"image_url","image_url":{"url":"https://media.example/chart.png","detail":"high"}})",
       R"({"type":"image_url","image_url":"https://media.example/chart.png"})"}) {
    Json chat, mapped;
    Q4T_CHECK(io::ParseJson(input, &chat));
    auto boundary = Object({{"mode", String("explicit")}});
    Set(&chat, "prompt_cache_breakpoint", boundary);
    Q4T_CHECK(ProjectChatMediaPart(chat, &mapped).ok());
    FileInputSource a, b;
    Q4T_CHECK(
        ParseImageInputSource(chat, FileInputProfile::kChat, "image", &a).ok());
    Q4T_CHECK(
        ParseImageInputSource(mapped, FileInputProfile::kResponses, "image", &b)
            .ok() &&
        a.value == b.value && a.kind == b.kind && a.detail == b.detail &&
        a.cache_breakpoint == b.cache_breakpoint);
    Q4T_CHECK(mapped.GetString("type") == "input_image" &&
              OutputJson(*mapped.Find("prompt_cache_breakpoint")) ==
                  OutputJson(boundary));
  }
  return true;
}

Q4T_TEST(evals_stored_chat_projects_snapshots_after_expanded_tool_history) {
  Fixture fixture;
  InputSnapshotRef alias;
  std::string projected_digest;
  StateAccess access{
      "client:alpha", std::chrono::steady_clock::now() + 10s, {}};
  BinaryAccess auth{access.scope, access.deadline, {}};
  {
    ObjectStore store(fixture.options);
    InputSnapshotStore snapshots(store);
    Json input, response;
    Q4T_CHECK(io::ParseJson(R"({"store":true,"messages":[
      {"role":"user","content":"Look up the records"},
      {"role":"assistant","content":null,"reasoning_content":"Read the source","tool_calls":[
        {"id":"call_one","type":"function","function":{"name":"read","arguments":"{}"}}]},
      {"role":"tool","tool_call_id":"call_one","content":"Document found"},
      {"role":"user","content":[{"type":"file","file":{"file_id":"file_original"}},
        {"type":"text","text":"Read the total"}]}]})",
                            &input));
    Q4T_CHECK(io::ParseJson(
        R"({"id":"chatcmpl_pdf","object":"chat.completion","model":"cpu",
      "created":1,"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"157"}}]})",
        &response));
    auto message = input.GetArray("messages")->array.back();
    Set(&message, "id", String(StoredChatMessageId("chatcmpl_pdf", 3)));
    const auto& part = message.GetArray("content")->array[0];
    std::string source_digest;
    Q4T_CHECK(InputValueIdentity(part, &source_digest));
    Json projected;
    Q4T_CHECK(ProjectChatMediaPart(part, &projected).ok() &&
              InputValueIdentity(projected, &projected_digest));
    Q4T_CHECK(source_digest != projected_digest);
    const std::string_view bytes = "immutable extracted page 157";
    InputSnapshotRef original;
    InputSnapshotHold hold;
    Q4T_CHECK(snapshots
                  .Save(auth, source_digest,
                        Object({{"origin", String("page")}}),
                        std::span(&bytes, 1), {}, 60, &original, &hold)
                  .ok());
    Json binding;
    Q4T_CHECK(
        BindInputSnapshot(message, "/content/0", original, &binding).ok());
    Q4T_CHECK(StoredChats(store)
                  .Save(input, response, access, Array({binding}))
                  .ok());
    Q4T_CHECK(snapshots.Release(auth, &hold).ok());
    auto def = Request(
        store, "POST", "/v1/evals",
        R"({"data_source_config":{"type":"logs"},"testing_criteria":[]})");
    Q4T_CHECK(def.ok());
    const auto runs = "/v1/evals/" + def.value.GetString("id") + "/runs";
    auto run = Request(
        store, "POST", runs,
        R"({"data_source":{"type":"completions","model":"cpu","source":{"type":"stored_completions"}}})");
    if (!run.ok())
      std::fprintf(stderr, "stored Chat projection: %s %s\n", run.code.c_str(),
                   run.message.c_str());
    Q4T_CHECK(run.ok() &&
              StoredChats(store).Erase("chatcmpl_pdf", access).ok());
    BinaryRead read;
    BinaryStore binary(store);
    Q4T_CHECK(binary
                  .Open(auth, ObjectKind::kEvalRun, run.value.GetString("id"),
                        60, &read)
                  .ok());
    std::string text;
    Q4T_CHECK(binary.Read(auth, read, 0, &text).ok() &&
              binary.Close(auth, &read).ok());
    Json row;
    Q4T_CHECK(io::ParseJson(text, &row, true));
    const auto* private_parts = row.GetArray("frozen_response_parts");
    Q4T_CHECK(private_parts && private_parts->array.size() == 1);
    const auto& indexed = private_parts->array.front();
    Q4T_CHECK(indexed.GetInt("index") == 4);
    const auto& mapped = *indexed.Find("binding");
    Q4T_CHECK(mapped.GetString("binding_sha256") == projected_digest &&
              ParseInputSnapshotReference(*mapped.Find("snapshot"), &alias) &&
              alias.id != original.id);
    auto fetch = [&](const InputSnapshotRef& root) {
      ObjectRequest get;
      get.scope = auth.scope;
      get.kind = ObjectKind::kInputSnapshot;
      get.id = root.id;
      return store.Execute(get, auth.deadline);
    };
    const auto old = fetch(original), copy = fetch(alias);
    Q4T_CHECK(old.ok() && copy.ok());
    auto& old_root = *old.value.Find("data");
    auto& new_root = *copy.value.Find("data");
    Q4T_CHECK(OutputJson(*old_root.Find("blob")) ==
                  OutputJson(*new_root.Find("blob")) &&
              new_root.GetArray("input_snapshots")
                      ->array[0]
                      .Find("snapshot")
                      ->GetString("id") == original.id);
    auto other = part;
    Set(&Field(other, "file"), "file_id", String("file_other"));
    InputSnapshotRef invalid;
    InputSnapshotHold invalid_hold;
    Q4T_CHECK(
        !snapshots
             .ProjectChat(auth, original, other, 60, &invalid, &invalid_hold)
             .ok() &&
        invalid.id.empty() && invalid_hold.id.empty());
  }
  ObjectStore reopened(fixture.options);
  InputSnapshotStore snapshots(reopened);
  InputSnapshot output;
  Q4T_CHECK(snapshots.Read(auth, alias, projected_digest, {}, &output).ok() &&
            output.parts.size() == 1 &&
            output.parts.front() == "immutable extracted page 157");
  BinaryAccess foreign{"client:beta", auth.deadline, {}};
  Q4T_CHECK(snapshots.Read(foreign, alias, projected_digest, {}, &output)
                .http_status == 404);
  return true;
}

Q4T_TEST(evals_stored_response_can_replay_its_input_after_source_erasure) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  StateAccess access{"client:alpha", std::chrono::steady_clock::now() + 10s, {}};
  ChatPlanConfig config;
  config.model = "cpu";
  ResponsesExecution original(objects);
  auto input = Object({{"input", String("Read source")}, {"store", Boolean(true)},
                       {"metadata", Object({{"source", String("log")}})}});
  Q4T_CHECK(original.Prepare(input, config, access, std::time(nullptr), true).ok());
  std::string wire;
  Q4T_CHECK(original.Begin(&wire).ok());
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = "157";
  Q4T_CHECK(original.Append(delta, "null", &wire));
  Q4T_CHECK(original.Finish(false, 0, {5,0,1,0}, std::time(nullptr), &wire).ok());
  auto def = Request(objects, "POST", "/v1/evals",
      R"({"data_source_config":{"type":"logs","metadata":{"source":"log"}},"testing_criteria":[]})");
  Q4T_CHECK(def.ok());
  const auto base = "/v1/evals/" + def.value.GetString("id") + "/runs";
  auto run = Request(objects, "POST", base, R"({"data_source":{"type":"responses","model":"cpu",
    "source":{"type":"responses","model":"cpu","metadata":{"source":"log"}}}})");
  Q4T_CHECK(run.ok());
  Q4T_CHECK(ResponsesState(objects).Erase(original.Id(), access).ok());
  BinaryAccess auth{access.scope,access.deadline,{}};
  BinaryRead read;
  BinaryStore store(objects);
  Q4T_CHECK(store.Open(auth,ObjectKind::kEvalRun,run.value.GetString("id"),60,&read).ok());
  Q4T_CHECK(read.manifest.chunks.size()==1);
  std::string bytes;
  Q4T_CHECK(store.Read(auth,read,0,&bytes).ok());
  Q4T_CHECK(store.Close(auth,&read).ok());
  Json row, request;
  Q4T_CHECK(io::ParseJson(bytes,&row,true));
  Q4T_CHECK(row.Find("item")->GetString("output_text")=="157");
  Q4T_CHECK(eval_detail::SamplingRequest(*run.value.Find("data_source"),row,config,&request).ok());
  const auto& message = request.GetArray("input")->array.front();
  Q4T_CHECK(!message.Has("id") && !message.Has("status"));
  ResponsesExecution replay(objects);
  Q4T_CHECK(replay.Prepare(request,config,access,std::time(nullptr),true).ok());
  return true;
}

Q4T_TEST(evals_tool_trajectory_preserves_calls_results_and_legacy_order) {
  Json input;
  Q4T_CHECK(io::ParseJson(R"([
    {"role":"user","content":[{"type":"text","text":"Read the project"}]},
    {"role":"assistant","content":null,"reasoning_content":"Check both sources","tool_calls":[
      {"id":"a","type":"function","function":{"name":"read_scope","arguments":"{\"path\":\"scope\"}"}},
      {"id":"b","type":"custom","custom":{"name":"receipt","input":"original bytes"}}]},
    {"role":"tool","tool_call_id":"b","content":"complete"},
    {"role":"tool","tool_call_id":"a","content":"157"},
    {"role":"user","content":"Which count did the tool return?"}
  ])", &input));
  Q4T_CHECK(eval_detail::NormalizeEvalTrajectory(&input).ok());
  Q4T_CHECK(input.array.size() == 7);
  Q4T_CHECK(input.array[0].GetArray("content")->array[0].GetString("type") == "input_text");
  Q4T_CHECK(input.array[1].GetArray("content")->array[0].GetString("text") == "Check both sources");
  Q4T_CHECK(input.array[2].GetString("arguments") == R"({"path":"scope"})" &&
            input.array[2].GetString("call_id") == "a");
  Q4T_CHECK(input.array[3].GetString("type") == "custom_tool_call" &&
            input.array[3].GetString("input") == "original bytes");
  Q4T_CHECK(input.array[4].GetString("type") == "custom_tool_call_output" &&
            input.array[4].GetString("call_id") == "b");
  Q4T_CHECK(input.array[5].GetString("output") == "157" &&
            input.array[5].GetString("call_id") == "a");
  Json historical;
  Q4T_CHECK(io::ParseJson(R"([{"role":"assistant","content":null,"function_call":{
    "name":"read_scope","arguments":"{}"}},{"role":"function","name":"read_scope","content":"157"}])",
    &historical));
  Q4T_CHECK(eval_detail::NormalizeEvalTrajectory(&historical).ok() &&
            historical.array.size() == 2 && !historical.array[0].GetString("call_id").empty() &&
            historical.array[0].GetString("call_id") == historical.array[1].GetString("call_id"));
  auto orphan = Array({Object({{"role", String("tool")}, {"tool_call_id", String("other")},
                                {"content", String("157")}})});
  Q4T_CHECK(eval_detail::NormalizeEvalTrajectory(&orphan).http_status == 400);
  return true;
}

Q4T_TEST(evals_grader_tool_variables_match_chat_choices_and_array_paths) {
  Json response;
  Q4T_CHECK(io::ParseJson(R"({"model":"cpu","status":"completed","output":[
    {"type":"function_call","id":"fc_private","call_id":"call_public","name":"read","arguments":"{}"},
    {"type":"custom_tool_call","call_id":"call_custom","name":"copy","input":"a b"}]})",
    &response));
  Json sample;
  Q4T_CHECK(eval_detail::ProjectEvalSample(response, Object(), &sample).ok());
  auto row = Object({{"sample", sample}});
  auto lookup = [&](const char* path) { return eval_detail::ResolveVariable(path, row); };
  const auto* function = lookup("sample.output_tools[0].function");
  const auto* custom = lookup("sample.choices[0].message.tool_calls.1.custom");
  Q4T_CHECK(function && function->GetString("name") == "read" &&
            function->GetString("arguments") == "{}" && custom && custom->GetString("input") == "a b");
  Q4T_CHECK(lookup("sample.output_tools[0].id")->str == "call_public" &&
            lookup("sample.choices[0].message.content")->IsNull() &&
            lookup("sample.choices.0.finish_reason")->str == "tool_calls");
  for (const auto path : {"sample.output_tools[-1]", "sample.output_tools[]",
                          "sample.output_tools[9]", "sample.output_tools[0]tail"})
    Q4T_CHECK(!lookup(path));
  Q4T_CHECK(io::ParseJson(R"({"status":"completed","output":[{"type":"message","role":"assistant",
    "content":[{"type":"output_text","text":"{}"}]}]})", &response));
  Q4T_CHECK(eval_detail::ProjectEvalSample(response, Object(), &sample).ok() && !sample.Has("output_json"));
  auto structured = Object({{"text", Object({{"format", Object({{"type", String("json_object")}})}})}});
  Q4T_CHECK(eval_detail::ProjectEvalSample(response, structured, &sample).ok() && sample.Find("output_json")->IsObject());
  return true;
}

Q4T_TEST(evals_model_samples_bind_one_worker_and_release_parent_and_child) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ClientAdmission clients;
  ChatPlanConfig config;
  config.model = "cpu";
  config.default_max_tokens = 32;
  auto client = clients.RecoveryIdentity(0);
  const std::string scope = "local:anonymous";
  StateAccess access{scope, std::chrono::steady_clock::now() + 20s, {}};
  std::atomic<int> calls{0};
  std::atomic<bool> deliver{false};
  ResponsesJobs jobs(objects, {}, [&](auto& job) {
    ++calls;
    while (!deliver && !job.control->Cancelled())
      std::this_thread::sleep_for(1ms);
    if (job.control->Cancelled()) return;
    ResponseDelta delta;
    delta.kind = ResponseDelta::Kind::kContent;
    delta.text = "157";
    std::string wire;
    if (!job.run->Append(delta, "null", &wire) ||
        !job.run->Finish(false, 0, {5, 0, 2, 0}, std::time(nullptr), &wire).ok())
      throw std::runtime_error("Evaluation sampling fixture");
  });
  eval_detail::SamplingOwner owner{clients, client, jobs, config};
  auto def = Request(objects, "POST", "/v1/evals", kDefinition, scope);
  Q4T_CHECK(def.ok());
  const auto base = "/v1/evals/" + def.value.GetString("id") + "/runs";
  const std::string body = R"({"data_source":{"type":"completions","model":"cpu",
    "input_messages":{"type":"template","template":[{"role":"user","content":"Report {{item.count}}"}]},
    "sampling_params":{"temperature":0,"reasoning_effort":"none"},
    "source":{"type":"file_content","content":[{"item":{"count":157}}]}}})";
  for (const bool cancel : {false, true}) {
    deliver = false;
    auto run = Request(objects, "POST", base, body, scope);
    Q4T_CHECK(run.ok());
    const auto& part = *run.value.Find("data_source")->Find("input_messages")->GetArray("template")->array[0].Find("content");
    Q4T_CHECK(part.GetString("type") == "input_text" && part.GetString("text") == "Report {{item.count}}");
    const auto id = run.value.GetString("id");
    const auto path = base + "/" + id;
    ObjectRequest get;
    get.kind = ObjectKind::kEvalRun;
    get.scope = scope;
    get.id = id;
    auto read = [&] { return objects.Execute(get, access.deadline); };
    auto advance = [&] {
      return eval_detail::AdvanceRun(objects, access, read().value, &owner);
    };
    Q4T_CHECK(advance().ok());
    const auto previous = read();
    auto claim = eval_detail::AdvanceRun(objects, access, previous.value, &owner);
    Q4T_CHECK(claim.http_status == 202);
    const auto claimed = read();
    auto data = *claimed.value.Find("data");
    const auto child = data.GetString("pending_response");
    Q4T_CHECK(!child.empty());
    const auto& input = *data.Find("sampling_request");
    Q4T_CHECK(input.GetInt("max_output_tokens") == 32 &&
              input.GetInt("seed", -1) >= 0 &&
              input.GetArray("input")->array[0].GetString("content") == "Report 157");
    Q4T_CHECK(advance().http_status == 202);
    auto stale = eval_detail::AdvanceRun(objects, access, previous.value, &owner);
    Q4T_CHECK(stale.http_status == 409);
    for (int i = 0; calls < (cancel ? 2 : 1) && i < 1000; ++i)
      std::this_thread::sleep_for(1ms);
    Q4T_CHECK(calls == (cancel ? 2 : 1));
    if (cancel) {
      // Simulate cancellation while the model owner is unavailable. A later
      // dispatcher observes the durable fence and releases the original child.
      auto unavailable = HandleEval(objects, access, "POST", path + "/cancel", {});
      Q4T_CHECK(unavailable.http_status == 503);
      auto fenced = Request(objects, "GET", path, {}, scope);
      Q4T_CHECK(fenced.ok() && fenced.value.GetString("status") == "canceled");
      Q4T_CHECK(advance().ok());
      Q4T_CHECK(Request(objects, "POST", path + "/cancel", {}, scope).ok());
    } else {
      deliver = true;
      for (int i = 0; jobs.Health().owned && i < 2000; ++i)
        std::this_thread::sleep_for(1ms);
      Q4T_CHECK(!jobs.Health().owned);
      Q4T_CHECK(ResponsesState(objects).Erase(child, access).http_status == 409);
      Q4T_CHECK(advance().ok());
      Q4T_CHECK(advance().ok());
      auto result = Request(objects, "GET", path, {}, scope);
      Q4T_CHECK(result.ok() && result.value.GetString("status") == "completed");
      Q4T_CHECK(result.value.Find("result_counts")->GetInt("passed") == 1);
      auto usage = result.value.GetArray("per_model_usage")->array.front();
      Q4T_CHECK(usage.GetInt("invocation_count") == 1 &&
                usage.GetInt("completion_tokens") == 2 &&
                usage.GetInt("total_tokens") == 7);
      auto outputs = Request(objects, "GET", path + "/output_items", {}, scope);
      Q4T_CHECK(outputs.ok() && outputs.value.GetArray("data")->array.size() == 1);
      const auto& sample = *outputs.value.GetArray("data")->array[0].Find("sample");
      Q4T_CHECK(sample.GetInt("seed") == input.GetInt("seed") &&
                sample.GetInt("max_completion_tokens") == 32);
    }
    Q4T_CHECK(!read().value.Find("data")->Has("pending_response"));
    Q4T_CHECK(ResponsesState(objects).Retrieve(child, access).http_status == 404);
    Q4T_CHECK(Request(objects, "DELETE", path, {}, scope).ok());
  }
  return true;
}

Q4T_TEST(
    evals_python_grades_use_isolated_worker_and_preserve_invalid_outcomes) {
  StateAccess access{
      "client:alpha", std::chrono::steady_clock::now() + 12s, {}};
  auto row = Object({{"item", Object({{"expected", String("157")}})},
                     {"sample", Object({{"output_text", String("157")}})}});
  auto grade = [&](std::string source) {
    auto python = Object({{"type", String("python")},
                          {"name", String("project")},
                          {"source", String(source)},
                          {"pass_threshold", Number(0)}});
    return eval_detail::GradePython(python, row, access);
  };
  auto passed = grade(
      "def grade(sample,item):\n print('unrelated output')\n return 0.75 if "
      "sample['output_text']==item['expected'] else 0.25");
  Q4T_CHECK(passed.ok() && passed.value.GetBool("passed") &&
            passed.value.GetNumber("score") == 0.75 &&
            !passed.value.Has("error"));
  for (const auto* source :
       {"def grade(sample,item):\n return float('nan')",
        "def grade(sample,item):\n raise ValueError('bad row')",
        "def grade(sample,item):\n return '1'"}) {
    auto invalid = grade(source);
    Q4T_CHECK(invalid.ok() && invalid.value.GetNumber("score") == 0 &&
              !invalid.value.GetBool("passed") &&
              invalid.value.Find("error")->GetString("code") ==
                  "invalid_python_grade");
  }
  return true;
}

Q4T_TEST(evals_interrupted_text_replays_once_with_original_input_and_seed) {
  for (const std::string mode : {"resume", "exhaust", "explicit", "tools", "cancel_parent", "drain_race"}) {
    Fixture fixture;
    ClientAdmission clients;
    auto identity = clients.RecoveryIdentity(0);
    ChatPlanConfig config;
    config.model = "cpu";
    config.structured = true;
    config.default_max_tokens = 32;
    StateAccess access{"local:anonymous",
                       std::chrono::steady_clock::now() + 10s, {}};
    ObjectRequest parent;
    parent.kind = ObjectKind::kEvalRun;
    parent.scope = access.scope;
    std::string path, first_id, frozen;
    std::atomic<unsigned> calls{0};
    for (unsigned attempt = 0; attempt < 2; ++attempt) {
      ObjectStore objects(fixture.options);
      ResponsesJobs jobs(objects, {}, [&](auto& job) {
        ++calls;
        if (!attempt && mode == "drain_race") {
          ResponsesExecution::ObserveError(job.run.get(), 503,
              "client minute quota unavailable", "service_draining");
          return;
        }
        if (!attempt || mode == "exhaust") {
          while (!job.control->Cancelled()) std::this_thread::sleep_for(1ms);
          return;
        }
        ResponseDelta delta;
        delta.kind = ResponseDelta::Kind::kContent;
        delta.text = "157";
        std::string wire;
        if (!job.run->Append(delta, "null", &wire) ||
            !job.run->Finish(false, 0, {5, 0, 2, 0}, std::time(nullptr),
                             &wire).ok())
          throw std::runtime_error("Evaluation retry fixture");
      });
      eval_detail::SamplingOwner owner{clients, identity, jobs, config};
      auto read = [&] { return objects.Execute(parent, access.deadline); };
      auto step = [&] {
        return eval_detail::AdvanceRun(objects, access, read().value, &owner);
      };
      if (!attempt) {
        auto def = Request(objects, "POST", "/v1/evals", kDefinition, access.scope);
        Q4T_CHECK(def.ok());
        const auto collection = "/v1/evals/" + def.value.GetString("id") + "/runs";
        std::string params = mode == "tools" ?
            R"("tools":[{"type":"function","function":{"name":"lookup","parameters":{"type":"object","properties":{}}}}],)" : "";
        auto run = Request(objects, "POST", collection,
            R"({"data_source":{"type":"completions","model":"cpu","input_messages":{"type":"template","template":[{"role":"user","content":"Report {{item.count}}"}]},"sampling_params":{)" +
            params + R"("temperature":0},"source":{"type":"file_content","content":[{"item":{"count":157}}]}}})",
            access.scope);
        Q4T_CHECK(run.ok());
        parent.id = run.value.GetString("id");
        path = collection + "/" + parent.id;
        Q4T_CHECK(step().ok());
      }
      const auto submitted = step();
      Q4T_CHECK(submitted.http_status == 202);
      const auto claimed = read();
      const auto& data = *claimed.value.Find("data");
      const auto child = data.GetString("pending_response");
      const auto* request = data.Find("sampling_request");
      Q4T_CHECK(!child.empty() && request);
      if (!attempt) {
        first_id = child;
        frozen = EncodeSchemaJson(*request);
      } else {
        Q4T_CHECK(first_id != child && EncodeSchemaJson(*request) == frozen &&
                  data.GetInt("interruption_retries") == 1);
      }
      for (int i = 0; calls != attempt + 1 && i < 1000; ++i)
        std::this_thread::sleep_for(1ms);
      Q4T_CHECK(calls == attempt + 1);
      if (mode == "explicit") Q4T_CHECK(jobs.Cancel(child, access).ok());
      if ((attempt && mode == "resume") || mode == "drain_race")
        for (int i = 0; jobs.Health().owned && i < 2000; ++i)
          std::this_thread::sleep_for(1ms);
      jobs.RequestStop();
      jobs.Join();
      Q4T_CHECK(step().ok());
      const auto saved = read();
      const auto& state = *saved.value.Find("data");
      if (mode == "explicit" || mode == "tools" || (attempt && mode == "exhaust")) {
        Q4T_CHECK(state.Find("resource")->GetString("status") == "failed");
        Q4T_CHECK(step().ok() && calls == attempt + 1);
        break;
      }
      if (!attempt) {
        Q4T_CHECK(!state.Has("pending_response") && state.Has("pending_input") &&
                  state.GetInt("row") == 0 && state.GetInt("interruption_retries") == 1);
        Q4T_CHECK(ResponsesState(objects).Retrieve(child, access).http_status == 404);
        if (mode == "cancel_parent") {
          auto stopped = Request(objects, "POST", path + "/cancel", {}, access.scope);
          Q4T_CHECK(stopped.ok() && stopped.value.GetString("status") == "canceled" &&
                    !read().value.Find("data")->Has("pending_input"));
          Q4T_CHECK(step().ok() && calls == 1);
          break;
        }
      } else {
        Q4T_CHECK(step().ok());
        auto result = Request(objects, "GET", path, {}, access.scope);
        Q4T_CHECK(result.ok() && result.value.GetString("status") == "completed" &&
                  result.value.Find("result_counts")->GetInt("passed") == 1);
      }
    }
  }
  return true;
}

Q4T_TEST(evals_frozen_service_preserves_queued_model_job_and_accepts_cancel) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  GatewayClientPolicy policy;
  policy.version = 3;
  policy.gateway_token = std::string(64, 'a');
  policy.sha256 = std::string(64, 'b');
  policy.clients.push_back({"alpha", 2, 1, 1024, 64, 65536, 30, 6000});
  ClientAdmission retiring;
  Q4T_CHECK(retiring.Configure(policy) && retiring.EnableRateRecovery() &&
            retiring.RestoreRates(nullptr, std::string(64, '1')));
  ChatPlanConfig config;
  config.model = "cpu";
  ResponsesJobs jobs(objects, {}, [](auto&) { throw std::logic_error("unexpected model callback"); });
  auto alpha = retiring.RecoveryIdentity(0);
  eval_detail::SamplingOwner owner{retiring, alpha, jobs, config};
  StateAccess access{"client:alpha", std::chrono::steady_clock::now() + 10s, {}};
  auto def = Request(objects, "POST", "/v1/evals", kDefinition);
  Q4T_CHECK(def.ok());
  auto path = "/v1/evals/" + def.value.GetString("id") + "/runs";
  auto run = Request(objects, "POST", path, R"({"data_source":{"type":"completions","model":"cpu",
    "source":{"type":"file_content","content":[{"item":{"count":157}}]}}})");
  Q4T_CHECK(run.ok());
  ObjectRequest get;
  get.kind = ObjectKind::kEvalRun;
  get.scope = access.scope;
  get.id = run.value.GetString("id");
  auto old = objects.Execute(get, access.deadline);
  ClientRateState checkpoint;
  Q4T_CHECK(retiring.FreezeRates(&checkpoint));
  for (int i = 0; i < 3; ++i)
    Q4T_CHECK(eval_detail::AdvanceRun(objects, access, old.value, &owner).http_status == 202);
  auto current = objects.Execute(get, access.deadline);
  Q4T_CHECK(EncodeSchemaJson(current.value) == EncodeSchemaJson(old.value) &&
            jobs.Health().owned == 0);
  auto canceled = Request(objects, "POST", path + "/" + get.id + "/cancel");
  Q4T_CHECK(canceled.ok() && canceled.value.GetString("status") == "canceled");
  return true;
}

Q4T_TEST(evals_model_graders_resume_without_replacing_the_original_sample) {
  Fixture f;
  std::string eval, id;
  std::atomic<int> calls{0};
  for (int phase = 0; phase < 2; ++phase) {
    ObjectStore store(f.options);
    ClientAdmission clients;
    ChatPlanConfig config;
    config.model = "cpu";
    config.structured = true;
    auto identity = clients.RecoveryIdentity(0);
    StateAccess auth{
        "local:anonymous", std::chrono::steady_clock::now() + 10s, {}};
    ResponsesJobs jobs(store, {}, [&](auto& job) {
      const auto* format =
          job.run->Envelope().options.Find("text")->Find("format");
      ++calls;
      ResponseDelta delta;
      delta.kind = ResponseDelta::Kind::kContent;
      delta.text = format->GetString("name") == "eval_label"
                       ? R"({"label":"right"})"
                       : R"({"result":0.75,"steps":[]})";
      std::string wire;
      if (!job.run->Append(delta, "null", &wire) ||
          !job.run->Finish(false, 0, {4, 0, 2, 0}, std::time(nullptr), &wire)
               .ok())
        throw std::runtime_error("Model grader fixture");
    });
    eval_detail::SamplingOwner owner{clients, identity, jobs, config};
    if (!phase) {
      auto definition = Request(store, "POST", "/v1/evals", R"({
        "data_source_config":{"type":"custom","item_schema":{"type":"object"}},
        "testing_criteria":[{"type":"label_model","model":"cpu","name":"Classify",
          "input":[{"role":"user","content":"Read {{sample.output_text}}"}],
          "labels":["right","wrong"],"passing_labels":["right"]},
          {"type":"score_model","model":"cpu","name":"Rate",
          "input":[{"role":"user","content":"Rate {{sample.output_text}}"}],
          "range":[0,1],"pass_threshold":0.8,"sampling_params":{"max_completions_tokens":48,"seed":15}}]})",
                                auth.scope);
      Q4T_CHECK(definition.ok());
      eval = "/v1/evals/" + definition.value.GetString("id") + "/runs";
      auto run = Request(store, "POST", eval,
                         R"({"data_source":{"type":"jsonl","source":{
        "type":"file_content","content":[{"item":{},"sample":{"output_text":"157"}}]}}})",
                         auth.scope);
      Q4T_CHECK(run.ok());
      id = run.value.GetString("id");
    }
    ObjectRequest get;
    get.kind = ObjectKind::kEvalRun;
    get.id = id;
    get.scope = auth.scope;
    bool reached = false;
    for (int step = 0; step < 1600 && !reached; ++step) {
      auto record = store.Execute(get, auth.deadline);
      Q4T_CHECK(record.ok());
      const auto& data = *record.value.Find("data");
      if (!phase) {
        const auto* grades = data.Find("model_results");
        reached = grades && grades->Has("0") && !data.Has("pending_response");
      } else {
        reached = data.Find("resource")->GetString("status") == "completed";
      }
      if (!reached) {
        auto advanced =
            eval_detail::AdvanceRun(store, auth, record.value, &owner);
        Q4T_CHECK(advanced.ok());
        std::this_thread::sleep_for(1ms);
      }
    }
    Q4T_CHECK(reached && calls == (phase ? 2 : 1));
    if (phase) {
      const auto path = eval + "/" + id;
      const auto final = Request(store, "GET", path, {}, auth.scope);
      Q4T_CHECK(final.ok() &&
                final.value.Find("result_counts")->GetInt("failed") == 1);
      const auto& usage = final.value.GetArray("per_model_usage")->array[0];
      Q4T_CHECK(usage.GetInt("invocation_count") == 2 &&
                usage.GetInt("total_tokens") == 12);
      auto page = Request(store, "GET", path + "/output_items", {}, auth.scope);
      Q4T_CHECK(page.ok() && page.value.GetArray("data")->array.size() == 1);
      const auto& row = page.value.GetArray("data")->array[0];
      Q4T_CHECK(row.Find("sample")->GetArray("output")->array[0].GetString(
                    "content") == "157");
      const auto& scores = row.GetArray("results")->array;
      Q4T_CHECK(scores.size() == 2 && scores[0].GetInt("score") == 1 &&
                scores[0].Find("sample")->GetString("label") == "right" &&
                scores[1].GetNumber("score") == 0.75 &&
                !scores[1].GetBool("passed"));
    }
  }
  return true;
}

Q4T_TEST(evals_text_similarity_matches_independent_lexical_oracles) {
  const auto row =
      Object({{"item", Object({{"reference", String("a b a b b c d")}})},
              {"sample", Object({{"output_text", String("a b a b c d")}})}});
  auto grade = [&](std::string metric, std::string input, std::string reference,
                   size_t work = 8 * 1024 * 1024) {
    size_t bytes = 1 << 20;
    auto threshold = Number(0);
    threshold.str.clear();
    threshold.number = 0.5;
    return eval_detail::GradeSimilarity(
        Object({{"name", String("comparison")},
                {"evaluation_metric", String(metric)},
                {"input", String(input)},
                {"reference", String(reference)},
                {"pass_threshold", std::move(threshold)}}),
        row, &bytes, &work);
  };
  // RapidFuzz normalized Indel, NLTK sentence BLEU/GLEU and Google rouge-score.
  for (const auto& [metric, expected] : std::array{
           std::pair{"fuzzy_match", 0.9166666666666665},
           std::pair{"bleu", 0.5985529678206387},
           std::pair{"gleu", 0.6818181818181818},
           std::pair{"rouge_1", 0.923076923076923},
           std::pair{"rouge_2", 0.9090909090909091},
           std::pair{"rouge_3", 0.6666666666666665},
           std::pair{"rouge_4", 0.28571428571428575}, std::pair{"rouge_5", 0.0},
           std::pair{"rouge_l", 0.923076923076923}}) {
    auto result = grade(metric, "{{sample.output_text}}", "{{item.reference}}");
    Q4T_CHECK(result.ok() &&
              std::abs(result.value.GetNumber("score") - expected) < 1e-12);
    Q4T_CHECK(result.value.GetBool("passed") == (expected >= 0.5));
  }
  Q4T_CHECK(
      std::abs(
          grade("fuzzy_match", "🍎甲乙", "🍎乙甲").value.GetNumber("score") -
          2.0 / 3.0) < 1e-12);
  Q4T_CHECK(grade("fuzzy_match", "", "").value.GetNumber("score") == 1.0);
  Q4T_CHECK(grade("rouge_l", "", "").value.GetNumber("score") == 0);
  Q4T_CHECK(
      grade("rouge_1", "RED, blue!", "red BLUE").value.GetNumber("score") == 1);
  Q4T_CHECK(grade("fuzzy_match", std::string(4096, 'x'), std::string(4096, 'y'),
                  16384)
                .http_status == 413);
  Q4T_CHECK(grade("fuzzy_match", "{{item.missing}}", "text").http_status ==
            400);
  return true;
}

Q4T_TEST(evals_string_grader_resolves_dataset_once_and_folds_unicode) {
  const auto row = Object(
      {{"sample", Object({{"output_text", String("Straße 157")}})},
       {"item", Object({{"stats", Array({Number(157)})},
                        {"literal", String("{{sample.output_text}}")}})}});
  auto check = [&](std::string operation, std::string input,
                   std::string reference, bool expected) {
    size_t remaining = 4096;
    auto result =
        eval_detail::GradeString(Object({{"name", String("Check")},
                                         {"input", String(input)},
                                         {"operation", String(operation)},
                                         {"reference", String(reference)}}),
                                 row, &remaining);
    return result.ok() && result.value.GetBool("passed") == expected &&
           result.value.GetInt("score") == (expected ? 1 : 0);
  };
  Q4T_CHECK(check("eq", "{{ item.stats.0 }}", "157", true));
  Q4T_CHECK(check("ne", "{{item.literal}}", "{{sample.output_text}}", true));
  Q4T_CHECK(check("like", "{{sample.output_text}}", "STRASSE", false));
  Q4T_CHECK(check("ilike", "{{sample.output_text}}", "STRASSE {{item.stats.0}}",
                  true));
  Q4T_CHECK(check("like", "100% reviewed", "%", true));
  Q4T_CHECK(check("eq", "x ", "x", false));
  Q4T_CHECK(check("like", "x", "", true));
  return true;
}

Q4T_TEST(evals_text_budget_and_missing_fields_never_publish_partial_text) {
  auto row = Object({{"item", Object({{"text", String("1234")}})}});
  for (const std::string name :
       {"{{item.missing}}", "{{other}}", "{{item.text", "{{item..text}}"}) {
    size_t budget = 128;
    std::string out = "previous";
    auto r = eval_detail::RenderText(name, row, "reference", &budget, &out);
    Q4T_CHECK(r.http_status == 400 &&
              r.value.GetString("param") == "reference");
    Q4T_CHECK(out == "previous");
  }
  size_t budget = 4;
  std::string out;
  auto full =
      eval_detail::RenderText("{{item.text}}", row, "input", &budget, &out);
  Q4T_CHECK(full.ok() && budget == 0 && out == "1234");
  auto overflow = eval_detail::RenderText("x", row, "input", &budget, &out);
  Q4T_CHECK(overflow.http_status == 413 && out == "1234");
  return true;
}

Q4T_TEST(evals_cosine_scores_the_actual_trained_pair_and_counts_usage) {
  auto criterion = Object({{"name", String("Meaning")},
                           {"input", String("{{sample.output_text}}")},
                           {"reference", String("{{item.reference}}")},
                           {"pass_threshold", Number(1)}});
  const auto row =
      Object({{"item", Object({{"reference", String("reference")}})},
              {"sample", Object({{"output_text", String("candidate")}})}});
  auto coords = Array();
  coords.array.resize(model::EmbeddingConfig::kHidden, Number(0));
  auto a = coords, b = coords;
  a.array[0] = Number(1);
  b.array[0] = Number(3);
  b.array[1] = Number(4);
  Json grade, resource = Object();
  const auto embed = [&](const Json& input) {
    ObjectReply result;
    if (input.array.size() != 2 || input.array[0].str != "candidate" ||
        input.array[1].str != "reference")
      return Error(400, "unexpected_pair", "Invalid pair");
    result.value =
        Object({{"object", String("list")},
                {"model", String(model::EmbeddingConfig::kModelId)},
                {"data", Array({Object({{"object", String("embedding")},
                                        {"index", Number(0)},
                                        {"embedding", a}}),
                                Object({{"object", String("embedding")},
                                        {"index", Number(1)},
                                        {"embedding", b}})})},
                {"usage", Object({{"prompt_tokens", Number(9)},
                                  {"total_tokens", Number(9)}})}});
    return result;
  };
  Q4T_CHECK(
      eval_detail::GradeCosine(criterion, row, embed, &grade, &resource).ok());
  Json wire;
  Q4T_CHECK(io::ParseJson(OutputJson(grade), &wire) &&
            !wire.GetBool("passed") &&
            std::abs(wire.GetNumber("score") - 0.6) < 1e-12);
  const auto& usage = resource.GetArray("per_model_usage")->array.front();
  Q4T_CHECK(usage.GetString("model_name") == model::EmbeddingConfig::kModelId &&
            usage.GetInt("invocation_count") == 1 &&
            usage.GetInt("total_tokens") == 9);
  a = b;
  Q4T_CHECK(
      eval_detail::GradeCosine(criterion, row, embed, &grade, &resource).ok() &&
      grade.GetNumber("score") == 1.0 && grade.GetBool("passed"));
  b.array[0] = Number(-3);
  b.array[1] = Number(-4);
  Q4T_CHECK(
      eval_detail::GradeCosine(criterion, row, embed, &grade, &resource).ok() &&
      grade.GetNumber("score") == 0.0 && !grade.GetBool("passed"));
  b = coords;
  Q4T_CHECK(
      eval_detail::GradeCosine(criterion, row, embed, &grade, &resource).code ==
      "eval_embedding_invalid");
  Q4T_CHECK(eval_detail::GradeCosine(criterion, row, {}, &grade, &resource)
                .http_status == 422);
  return true;
}

constexpr char kCosineDefinition[] = R"({
  "data_source_config":{"type":"custom","item_schema":{"type":"object"}},
  "testing_criteria":[{"type":"text_similarity","name":"Semantic",
    "evaluation_metric":"cosine","pass_threshold":1,"input":"{{sample.output_text}}",
    "reference":"{{item.reference}}"}]})";
constexpr char kCosineRows[] = R"({"data_source":{"type":"jsonl","source":{
  "type":"file_content","content":[{"item":{"reference":"Project scope"},
  "sample":{"output_text":"Project scope"}}]}}})";

Q4T_TEST(evals_cosine_dispatcher_cancels_only_the_matching_project_and_run) {
  Fixture f;
  ObjectStore store(f.options);
  ClientAdmission clients;
  const std::string scope = "local:anonymous";
  const auto definition =
      Request(store, "POST", "/v1/evals", kCosineDefinition, scope);
  Q4T_CHECK(definition.ok());
  const auto eval = definition.value.GetString("id");
  const auto runs = "/v1/evals/" + eval + "/runs";
  const auto created = Request(store, "POST", runs, kCosineRows, scope);
  Q4T_CHECK(created.ok());
  const auto run = created.value.GetString("id");
  ResponsesJobs jobs(store, {}, [](auto&) {});
  std::atomic<std::shared_ptr<RequestControl>> active;
  std::atomic<int> calls{0};
  EvalDispatcher dispatcher(
      store, clients, {}, [&] { return &jobs; },
      [&](const Json&, const auto&, auto&, const auto& control) {
        ++calls;
        active.store(control);
        while (!control->Cancelled()) std::this_thread::sleep_for(1ms);
        return Error(409, "eval_canceled", "Embedding canceled");
      },
      5s);
  const auto end = std::chrono::steady_clock::now() + 4s;
  while (!active.load() && std::chrono::steady_clock::now() < end)
    std::this_thread::sleep_for(1ms);
  auto owned = active.load();
  Q4T_CHECK(owned && !owned->Cancelled());
  dispatcher.CancelEmbedding("client:beta", eval, run);
  dispatcher.CancelEmbedding(scope, eval, "other");
  Q4T_CHECK(!owned->Cancelled());
  auto canceled =
      Request(store, "POST", runs + "/" + run + "/cancel", {}, scope);
  Q4T_CHECK(canceled.ok());
  dispatcher.CancelEmbedding(scope, eval, run);
  Q4T_CHECK(owned->Cancelled());
  dispatcher.RequestStop();
  dispatcher.Join();
  const auto final = Request(store, "GET", runs + "/" + run, {}, scope);
  Q4T_CHECK(final.ok() && final.value.GetString("status") == "canceled" &&
            final.value.Find("result_counts")->GetInt("total") == 0 &&
            calls == 1);
  return true;
}

Q4T_TEST(evals_cosine_recovers_checkpoint_and_counts_missing_model_as_error) {
  Fixture f;
  ObjectRequest read;
  read.kind = ObjectKind::kEvalRun;
  read.scope = "local:anonymous";
  std::string runs;
  int calls = 0;
  for (int phase = 0; phase < 4; ++phase) {
    ObjectStore store(f.options);
    ClientAdmission clients;
    ChatPlanConfig config;
    StateAccess auth{read.scope, std::chrono::steady_clock::now() + 5s, {}};
    ResponsesJobs jobs(store, {}, [](auto&) {});
    eval_detail::SamplingOwner owner{clients, clients.RecoveryIdentity(0), jobs,
                                     config};
    if (phase == 0) {
      auto def =
          Request(store, "POST", "/v1/evals", kCosineDefinition, read.scope);
      Q4T_CHECK(def.ok());
      runs = "/v1/evals/" + def.value.GetString("id") + "/runs";
      owner.embed = [&](const Json&) {
        ++calls;
        auto vector = Array();
        vector.array.resize(model::EmbeddingConfig::kHidden, Number(1));
        auto data = Array();
        for (int i = 0; i < 2; ++i)
          data.array.push_back(Object({{"object", String("embedding")},
                                       {"index", Number(i)},
                                       {"embedding", vector}}));
        ObjectReply result;
        result.value =
            Object({{"object", String("list")},
                    {"model", String(model::EmbeddingConfig::kModelId)},
                    {"data", data},
                    {"usage", Object({{"total_tokens", Number(7)},
                                      {"prompt_tokens", Number(7)}})}});
        return result;
      };
    }
    if (phase == 3)
      owner.embed = [](const Json&) {
        return Error(409, "deadline_exceeded", "Embedding deadline elapsed");
      };
    if (phase != 1) {
      auto run = Request(store, "POST", runs, kCosineRows, read.scope);
      Q4T_CHECK(run.ok());
      read.id = run.value.GetString("id");
    }
    bool reached = false;
    for (int step = 0; step < 10 && !reached; ++step) {
      auto record = store.Execute(read, auth.deadline);
      Q4T_CHECK(record.ok());
      const auto& data = *record.value.Find("data");
      const auto* grades = data.Find("model_results");
      reached = !phase
                    ? grades && grades->Has("0")
                    : data.Find("resource")->GetString("status") == "completed";
      if (!reached)
        Q4T_CHECK(
            eval_detail::AdvanceRun(store, auth, record.value, &owner).ok());
    }
    Q4T_CHECK(reached && calls == 1);
    if (!phase) continue;
    const auto path = runs + "/" + read.id;
    auto final = Request(store, "GET", path, {}, read.scope);
    Q4T_CHECK(final.ok());
    const auto& counts = *final.value.Find("result_counts");
    const auto* usage = final.value.Find("per_model_usage");
    Q4T_CHECK(usage);
    Q4T_CHECK(counts.GetInt("total") == 1 && counts.GetInt("failed") == 0);
    if (phase == 1) {
      Q4T_CHECK(counts.GetInt("passed") == 1 && usage->IsArray() &&
                usage->array.size() == 1 &&
                usage->array[0].GetInt("invocation_count") == 1 &&
                usage->array[0].GetInt("total_tokens") == 7);
    } else {
      auto page = Request(store, "GET", path + "/output_items", {}, read.scope);
      Q4T_CHECK(page.ok() && counts.GetInt("errored") == 1 && usage->IsNull());
      const auto& output = page.value.GetArray("data")->array.front();
      Q4T_CHECK(output.GetString("status") == "error" &&
                output.GetArray("results")->array.empty() &&
                output.Find("sample")->Find("error")->GetString("code") ==
                    (phase == 2 ? "eval_embedding_model_unconfigured"
                                : "deadline_exceeded"));
    }
  }
  return true;
}

Q4T_TEST(evals_report_origin_follows_the_authenticated_request) {
  HttpRequestHeaders h;
  h.host = "localhost:8100";
  h.forwarded_host = "scope.example:8443";
  h.forwarded_proto = "https";
  h.forwarded_prefix = "/tenant/api";
  Q4T_CHECK(EvalReportBase(h, false, {}, {}) == "http://localhost:8100");
  Q4T_CHECK(EvalReportBase(h, true, {}, {}) ==
            "https://scope.example:8443/tenant/api");
  Q4T_CHECK(EvalReportBase(h, false, {}, "https://public.example/service") ==
            "https://public.example/service");
  h.forwarded_prefix = "/tenant/../other";
  Q4T_CHECK(EvalReportBase(h, true, {}, {}).empty());
  h = {};
  Q4T_CHECK(EvalReportBase(h, false, "[::1]:8123", {}) == "http://[::1]:8123");
  for (const auto* host : {"host/other", "user@host", "host:70000",
                           "localhost?query", "host#fragment"}) {
    h.host = host;
    Q4T_CHECK(EvalReportBase(h, false, {}, {}).empty());
  }
  return true;
}

Q4T_TEST(evals_report_bounds_large_unicode_names_and_many_criteria) {
  auto grades = Array();
  for (int i = 0; i < 101; ++i)
    grades.array.push_back(Object({{"testing_criteria", String("Grade " +
        std::to_string(i))}, {"passed", Number(i)}, {"failed", Number(0)}}));
  auto report = eval_detail::RenderRunReport(Object({
      {"id", String("evalrun_r")}, {"eval_id", String("eval_e")},
      {"name", String(std::string(1023, 'a') + "演示")},
      {"per_testing_criteria_results", std::move(grades)}}));
  Q4T_CHECK(report.find(std::string(1023, 'a') + "…</h2>") != report.npos);
  Q4T_CHECK(report.find("Grade 99</th>") != report.npos);
  Q4T_CHECK(report.find("Grade 100</th>") == report.npos);
  Q4T_CHECK(report.find("Showing the first 100") != report.npos);
  return true;
}

Q4T_TEST(evals_runs_persist_real_pass_fail_outputs_and_filter_by_parent) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  auto definition = Request(store, "POST", "/v1/evals", kDefinition);
  Q4T_CHECK(definition.ok());
  const auto eval = "/v1/evals/" + definition.value.GetString("id");
  const auto collection = eval + "/runs";
  const std::string body =
      R"({"name":"Project <draft> & review","data_source":{"type":"jsonl","source":{
    "type":"file_content","content":[{"item":{"count":157},"sample":{"output_text":"157"}},
    {"item":{"count":88},"sample":{"output_text":"87"}}]}}})";
  auto created = Request(store, "POST", collection, body);
  if (!created.ok())
    std::fprintf(stderr, "eval run create %d %s: %s (%s)\n", created.http_status,
                 created.code.c_str(), created.message.c_str(),
                 created.value.GetString("param").c_str());
  Q4T_CHECK(created.ok() && created.http_status == 201);
  const auto id = created.value.GetString("id");
  const auto path = collection + "/" + id;
  Q4T_CHECK(created.value.GetString("status") == "queued");
  Q4T_CHECK(Request(store, "GET", path, {}, "client:beta").http_status == 404);
  Q4T_CHECK(Request(store, "GET", path + "/report", {}, "client:beta")
                .http_status == 404);
  Q4T_CHECK(Request(store, "POST", path + "/report", "{}").http_status == 405);
  Q4T_CHECK(Request(store, "GET", path + "/report?limit=1").http_status == 400);
  const auto before = Request(store, "GET", collection + "?status=queued");
  Q4T_CHECK(before.ok() && before.value.GetArray("data")->array.size() == 1);
  StateAccess access{
      "client:alpha", std::chrono::steady_clock::now() + 10s, {}};
  auto listed = HandleEval(store, access, "GET", collection, {}, nullptr,
                           "https://first.example/prefix");
  Q4T_CHECK(listed.ok() && listed.value.GetArray("data")->array[0]
      .GetString("report_url") == "https://first.example/prefix" + path + "/report");
  auto redirected = HandleEval(store, access, "GET", path, {}, nullptr,
                               "https://second.example");
  Q4T_CHECK(redirected.value.GetString("report_url") ==
            "https://second.example" + path + "/report");
  ObjectRequest read;
  read.kind = ObjectKind::kEvalRun;
  read.scope = access.scope;
  read.id = id;
  // rev480 stored a relative JSON collection in the durable run. Opening
  // that record through a new frontend must only project the public view.
  auto old = store.Execute(read, access.deadline);
  Q4T_CHECK(old.ok());
  auto previous = read;
  previous.operation = ObjectOperation::kPut;
  previous.expected_revision = old.value.GetInt("revision");
  previous.expires_at = old.value.GetInt("expires_at");
  previous.data = *old.value.Find("data");
  Set(&Field(previous.data, "resource"), "report_url", String(path + "/output_items"));
  Q4T_CHECK(store.Execute(previous, access.deadline).ok());
  auto historical = HandleEval(store, access, "GET", path, {}, nullptr, "https://old.example");
  Q4T_CHECK(historical.ok() && historical.value.GetString("report_url") ==
            "https://old.example" + path + "/report");
  auto stored = store.Execute(read, access.deadline);
  Q4T_CHECK(stored.value.Find("data")->Find("resource")->GetString("report_url") ==
            path + "/output_items");
  for (int turn = 0; turn < 4; ++turn) {
    auto record = store.Execute(read, access.deadline);
    Q4T_CHECK(record.ok());
    auto step = eval_detail::AdvanceRun(store, access, record.value);
    Q4T_CHECK(step.ok());
    if (turn == 1) {
      auto stale = eval_detail::AdvanceRun(store, access, record.value);
      Q4T_CHECK(stale.http_status == 409);
    }
  }
  auto final = Request(store, "GET", path);
  Q4T_CHECK(final.ok() && final.value.GetString("status") == "completed");
  const auto& counts = Field(final.value, "result_counts");
  Q4T_CHECK(counts.GetInt("passed") == 1 && counts.GetInt("failed") == 1 &&
            counts.GetInt("total") == 2 && counts.GetInt("errored") == 0);
  auto report = Request(store, "GET", path + "/report");
  Q4T_CHECK(report.ok() && report.value.IsString() &&
            report.value.str.find("Project &lt;draft&gt; &amp; review") !=
                std::string::npos &&
            report.value.str.find("Count check</th><td>1</td><td>1") !=
                std::string::npos);
  auto outputs = Request(store, "GET", path + "/output_items?limit=1");
  Q4T_CHECK(outputs.ok() && outputs.value.GetBool("has_more"));
  const auto first = outputs.value.GetString("last_id");
  auto item = Request(store, "GET", path + "/output_items/" + first);
  Q4T_CHECK(item.ok() && item.value.GetString("status") == "pass" &&
            item.value.Find("datasource_item")->GetInt("count") == 157);
  auto fails = Request(store, "GET",
                       path + "/output_items?after=" + first + "&status=fail");
  Q4T_CHECK(fails.ok() && fails.value.GetArray("data")->array.size() == 1);
  const auto& failed = fails.value.GetArray("data")->array[0];
  Q4T_CHECK(failed.GetInt("datasource_item_id") == 1 &&
            failed.GetArray("results")->array[0].GetInt("score") == 0);
  Q4T_CHECK(Request(store, "POST", path + "/cancel").http_status == 409);
  auto another = Request(store, "POST", collection, body);
  Q4T_CHECK(another.ok());
  auto second = collection + "/" + another.value.GetString("id");
  Q4T_CHECK(
      Request(store, "GET", second + "/output_items/" + first).http_status ==
      404);
  Q4T_CHECK(Request(store, "GET", second + "/output_items?after=" + first)
                .http_status == 400);
  auto cancel = Request(store, "POST", second + "/cancel");
  Q4T_CHECK(cancel.ok() && cancel.value.GetString("status") == "canceled");
  Q4T_CHECK(Request(store, "POST", second + "/cancel").ok());
  auto gone = Request(store, "DELETE", path);
  Q4T_CHECK(gone.ok() && gone.value.GetString("run_id") == id);
  Q4T_CHECK(Request(store, "GET", path).http_status == 404);
  read.kind = ObjectKind::kEvalOutput;
  read.id = first;
  Q4T_CHECK(store.Execute(read, access.deadline).http_status == 404);
  Q4T_CHECK(Request(store, "DELETE", eval).ok());
  read.kind = ObjectKind::kEvalRun;
  read.id = another.value.GetString("id");
  Q4T_CHECK(store.Execute(read, access.deadline).http_status == 404);
  return true;
}

Q4T_TEST(evals_file_run_keeps_its_input_after_file_deletion_and_reopen) {
  Fixture fixture;
  std::string eval, run, id, file;
  auto deadline = [] { return std::chrono::steady_clock::now() + 10s; };
  {
    ObjectStore store(fixture.options);
    auto def = Request(store, "POST", "/v1/evals", kDefinition);
    Q4T_CHECK(def.ok());
    eval = "/v1/evals/" + def.value.GetString("id");
    BinaryAccess access{"client:alpha", deadline(), {}};
    FileUpload upload(store, access, "multipart/form-data; boundary=f");
    const std::string wire =
        "--f\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nevals"
        "\r\n--f\r\nContent-Disposition: form-data; name=\"file\"; "
        "filename=\"records.jsonl\"\r\n\r\n"
        "{\"item\":{\"count\":157},\"sample\":{\"output_text\":\"157\"}}\r\n"
        "{\"item\":{\"count\":88},\"sample\":{\"output_text\":\"88\"}}\r\n--f--"
        "\r\n";
    Q4T_CHECK(upload.Feed(wire, deadline()).ok());
    auto done = upload.Finish();
    Q4T_CHECK(done.ok());
    file = done.value.GetString("id");
    auto body = OutputJson(
        Object({{"data_source",
                 Object({{"type", String("jsonl")},
                         {"source", Object({{"type", String("file_id")},
                                            {"id", String(file)}})}})}}));
    auto foreign =
        Request(store, "POST", "/v1/evals", kDefinition, "client:beta");
    Q4T_CHECK(foreign.ok());
    const auto foreign_path =
        "/v1/evals/" + foreign.value.GetString("id") + "/runs";
    Q4T_CHECK(
        Request(store, "POST", foreign_path, body, "client:beta").http_status ==
        404);
    auto created = Request(store, "POST", eval + "/runs", body);
    Q4T_CHECK(created.ok());
    id = created.value.GetString("id");
    run = eval + "/runs/" + id;
    Q4T_CHECK(Files(store).Erase(access, file).ok());
    auto edit =
        Request(store, "POST", eval, R"({"metadata":{"review":"complete"}})");
    Q4T_CHECK(edit.ok());
    Q4T_CHECK(Request(store, "GET", eval + "/runs")
                  .value.GetArray("data")
                  ->array.size() == 1);
  }
  ObjectStore reopened(fixture.options);
  ObjectRequest read;
  read.scope = "client:alpha";
  read.kind = ObjectKind::kEvalRun;
  read.id = id;
  StateAccess access{read.scope, deadline(), {}};
  for (int step = 0; step < 4; ++step) {
    auto record = reopened.Execute(read, deadline());
    Q4T_CHECK(record.ok());
    auto result = eval_detail::AdvanceRun(reopened, access, record.value);
    Q4T_CHECK(result.ok());
  }
  auto finished = Request(reopened, "GET", run);
  Q4T_CHECK(finished.ok() && finished.value.GetString("status") == "completed");
  Q4T_CHECK(finished.value.Find("result_counts")->GetInt("passed") == 2);
  auto removed = Request(reopened, "DELETE", eval);
  Q4T_CHECK(removed.ok() &&
            reopened.Execute(read, deadline()).http_status == 404);
  return true;
}

Q4T_TEST(evals_definitions_reopen_isolate_update_and_delete) {
  Fixture fixture;
  std::string path;
  {
    ObjectStore objects(fixture.options);
    auto created = Request(objects, "POST", "/v1/evals", kDefinition);
    Q4T_CHECK(created.ok() && created.http_status == 201);
    const auto id = created.value.GetString("id");
    Q4T_CHECK(id.starts_with("eval_"));
    path = "/v1/evals/" + id;
    const auto& source = Field(created.value, "data_source_config");
    const auto* schema = source.Find("schema");
    Q4T_CHECK(schema && schema->GetArray("required")->array.size() == 2);
    Q4T_CHECK(schema->Find("properties")
                  ->Find("item")
                  ->Find("properties")
                  ->Has("count"));
    Q4T_CHECK(schema->Find("properties")
                  ->Find("sample")
                  ->Find("properties")
                  ->Has("choices"));
  }
  ObjectStore objects(fixture.options);
  auto prior = Request(objects, "GET", path);
  Q4T_CHECK(prior.ok() && prior.value.GetString("name") == "Scope report");
  for (const char* verb : {"GET", "POST", "DELETE"})
    Q4T_CHECK(Request(objects, verb, path,
                      std::string_view(verb) == "POST" ? "{}" : "",
                      "client:other")
                  .http_status == 404);
  auto updated = Request(objects, "POST", path,
                         R"({"name":"Published scope","metadata":null})");
  Q4T_CHECK(updated.ok() && updated.value.Find("metadata")->IsNull());
  Q4T_CHECK(updated.value.GetInt("created_at") ==
            prior.value.GetInt("created_at"));
  Q4T_CHECK(OutputJson(*updated.value.Find("testing_criteria")) ==
            OutputJson(*prior.value.Find("testing_criteria")));
  auto removed = Request(objects, "DELETE", path);
  Q4T_CHECK(removed.ok() && removed.value.GetBool("deleted") &&
            removed.value.GetString("eval_id") == prior.value.GetString("id"));
  Q4T_CHECK(Request(objects, "GET", path).http_status == 404);
  return true;
}

Q4T_TEST(evals_list_orders_by_actual_update_and_scopes_cursors) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  auto a = Request(objects, "POST", "/v1/evals", kDefinition);
  auto b = Request(objects, "POST", "/v1/evals", kDefinition);
  Q4T_CHECK(a.ok() && b.ok());
  const auto first = a.value.GetString("id"), second = b.value.GetString("id");
  Q4T_CHECK(
      Request(objects, "POST", "/v1/evals/" + first, R"({"name":"new"})").ok());
  auto page = Request(objects, "GET", "/v1/evals?limit=1");
  Q4T_CHECK(page.ok() && page.value.GetString("first_id") == first &&
            page.value.GetBool("has_more"));
  page = Request(objects, "GET", "/v1/evals?order_by=updated_at&limit=1");
  Q4T_CHECK(page.ok() && page.value.GetString("last_id") == second &&
            page.value.GetBool("has_more"));
  page =
      Request(objects, "GET", "/v1/evals?order_by=updated_at&after=" + second);
  Q4T_CHECK(page.ok() && page.value.GetString("first_id") == first &&
            !page.value.GetBool("has_more"));
  page = Request(objects, "GET", "/v1/evals?order=desc&order_by=updated_at");
  Q4T_CHECK(page.ok() && page.value.GetString("first_id") == first);
  page = Request(objects, "GET", "/v1/evals", "", "client:empty");
  Q4T_CHECK(page.ok() && page.value.GetArray("data")->array.empty() &&
            page.value.Find("last_id")->IsString());
  Q4T_CHECK(
      Request(objects, "GET", "/v1/evals?after=" + first, "", "client:empty")
          .http_status == 400);
  return true;
}

Q4T_TEST(evals_definitions_validate_http_grader_schema_and_sources) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  for (
      const char* body :
      {R"({"data_source_config":{"type":"custom"},"testing_criteria":[]})",
       R"({"data_source_config":{"type":"custom","item_schema":{"type":"invalid"}},"testing_criteria":[]})",
       R"({"data_source_config":{"type":"logs"},"testing_criteria":[{"type":"string_check"}]})",
       R"({"data_source_config":{"type":"logs","metadata":{"x":12}},"testing_criteria":[]})",
       R"({"data_source_config":{"type":"logs"},"testing_criteria":[],"extra":true})"})
    Q4T_CHECK(Request(objects, "POST", "/v1/evals", body).http_status == 400);
  for (const char* type : {"logs", "stored_completions"}) {
    auto body = Object(
        {{"data_source_config",
          Object({{"type", String(type)},
                  {"metadata", Object({{"project", String("scope")}})}})},
         {"testing_criteria", Array()}});
    auto created = Request(objects, "POST", "/v1/evals", OutputJson(body));
    Q4T_CHECK(created.ok());
    const auto& config = Field(created.value, "data_source_config");
    Q4T_CHECK(config.GetString("type") == type &&
              config.Find("schema")->Find("properties")->Has("sample"));
    Q4T_CHECK(config.Find("metadata")->GetString("project") == "scope");
  }
  for (const char* query : {"limit=-1", "order_by=invented", "after=x&after=y",
                            "limit=3.1", "unexpected=1"})
    Q4T_CHECK(Request(objects, "GET", std::string("/v1/evals?") + query)
                  .http_status == 400);
  Q4T_CHECK(Request(objects, "DELETE", "/v1/evals").http_status == 405);
  Q4T_CHECK(!IsEvalTarget("/v1/evals/eval_one/another") &&
            !IsEvalTarget("/v1/evals/eval_one#bad"));
  Q4T_CHECK(IsEvalObservation("GET", "/v1/evals") &&
            !IsEvalObservation("POST", "/v1/evals"));
  return true;
}
}  // namespace
}  // namespace q4t::server
